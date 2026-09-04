"""Agent Activity screen - PROJECT_SPEC.md section 17.3/18: real live
state/tool transitions. POST /api/runs triggers a real run of
app.orchestrator.batch_runner.run_batch (UNCHANGED) against a fresh DB
session in a background thread - the actual production entry point, not a
simulation. GET .../stream tails the real AgentEvent rows it writes as it
writes them (by polling Postgres for new rows - no change to
app.orchestrator.events.emit_event was needed or made). Opening the stream
for an already-completed batch just fast-drains its historical event trail
and sends `done` - the same endpoint serves both "watch it happen live"
and "replay what happened", per section 21's "the difficult demo case can
be replayed convincingly"."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.common import require_visible_batch, serialize_events
from app.api.routes_auth import get_current_user
from app.api.schemas import AgentEventItem, RunRequest, RunStatus
from app.core.config import settings
from app.datagen.models import GeneratedBatch
from app.db.session import SessionLocal, get_db
from app.matcher.db_adapter import load_dataset
from app.models.auth import User
from app.models.operational import AgentEvent, Batch, ReconciliationCase
from app.orchestrator.batch_runner import run_batch
from app.rootcause.client import AnthropicRootCauseClient, GeminiRootCauseClient, RootCauseLLMClient

router = APIRouter(prefix="/api/runs", tags=["runs"])


@dataclass
class _RunState:
    dataset_version: str
    running: bool = True
    total: int | None = None
    resolved: int = 0
    escalated: int = 0
    errors: int = 0
    error_message: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_RUNS: dict[str, _RunState] = {}


class _ReferenceStandInClient:
    """Identical reasoning to scripts/run_orchestrator.py's stand-in,
    duplicated per this project's established per-script pattern. Used
    only when ANTHROPIC_API_KEY is not set. Never reads ground truth."""

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = json.loads(user_prompt)
        stage, delta, evidence = payload["divergence_stage"], payload["delta"], payload["evidence"]
        narration_text = " ".join((e.get("narration") or "") for e in evidence if e.get("type") == "bank_transaction").upper()
        if stage == "settlement":
            for e in evidence:
                if e.get("type") == "refund":
                    if delta == e["amount_paisa"]:
                        return json.dumps({"root_cause": "missing_refund_netting", "supporting_evidence": [e["id"]], "confidence": 0.85, "explanation": "delta matches a refund exactly, unnetted"})
                    if delta == -e["amount_paisa"]:
                        return json.dumps({"root_cause": "duplicate_refund", "supporting_evidence": [e["id"]], "confidence": 0.85, "explanation": "delta matches a refund exactly, netted twice"})
            if 0 < abs(delta) <= 5:
                return json.dumps({"root_cause": "currency_rounding", "supporting_evidence": [], "confidence": 0.80, "explanation": "delta within a rounding-sized band"})
        bank_ids = [e["id"] for e in evidence if e.get("type") == "bank_transaction"]
        if "PROC CHG" in narration_text or "ADDL" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids, "confidence": 0.85, "explanation": "bank narration references an additional charge"})
        if "BANK CHARGES" in narration_text or "NET OF" in narration_text:
            return json.dumps({"root_cause": "unmatched_external_deduction", "supporting_evidence": bank_ids, "confidence": 0.85, "explanation": "bank narration references bank-side charges"})
        if "(DUP)" in narration_text:
            return json.dumps({"root_cause": "duplicate_bank_credit", "supporting_evidence": bank_ids, "confidence": 0.90, "explanation": "more than one bank transaction references this settlement"})
        if "ADJ" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids, "confidence": 0.45, "explanation": "vague adjustment narration, insufficient to be confident"})
        return json.dumps({"root_cause": "unknown", "supporting_evidence": [], "confidence": 0.20, "explanation": "no supporting evidence found"})


def _ai_client() -> RootCauseLLMClient:
    # Gemini is the real provider (see app/rootcause/client.py) - same
    # precedence as scripts/run_orchestrator.py, run_evaluation.py, and
    # gemini_smoke_test.py. This function previously only checked
    # anthropic_api_key, silently skipping Gemini even when configured -
    # fixed here to match every other real-run entry point in the project.
    if settings.gemini_api_key:
        return GeminiRootCauseClient(settings.gemini_api_key, settings.gemini_model)
    if settings.anthropic_api_key:
        return AnthropicRootCauseClient(settings.anthropic_api_key, settings.anthropic_model)
    return _ReferenceStandInClient()


def _purge(session: Session, batch_id: str) -> None:
    from sqlalchemy import delete

    from app.models.operational import ExceptionRecord, Investigation, Match
    case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
    if not case_ids:
        return
    for model in (AgentEvent, Match, Investigation, ExceptionRecord):
        session.execute(delete(model).where(model.case_id.in_(case_ids)))
    session.execute(delete(ReconciliationCase).where(ReconciliationCase.case_id.in_(case_ids)))
    session.commit()


def _run_in_background(dataset_version: str, batch_id: str, state: _RunState) -> None:
    session = SessionLocal()
    try:
        orders, payments, refunds, settlements, bank_txns = load_dataset(session, dataset_version)
        if not orders:
            with state.lock:
                state.running, state.error_message = False, f"no records for dataset_version={dataset_version!r}"
            return
        _purge(session, batch_id)
        with state.lock:
            state.total = len(orders)
        batch = GeneratedBatch(batch_id=batch_id, dataset_version=dataset_version, seed=0,
                                orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)
        summary = run_batch(session, batch, _ai_client())
        with state.lock:
            state.resolved, state.escalated, state.errors = summary.resolved, summary.escalated, len(summary.errors)
    except Exception as exc:  # noqa: BLE001 - report to the console instead of losing the thread silently
        with state.lock:
            state.error_message = str(exc)
    finally:
        with state.lock:
            state.running = False
            state.finished_at = time.time()
        session.close()


@router.post("", response_model=RunStatus)
def trigger_run(req: RunRequest, session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> RunStatus:
    batch_id = f"batch_{req.dataset_version}"
    # A pre-existing Batch row (e.g. created by app.api.routes_import)
    # must belong to this user (or be system/NULL-owned) before they can
    # re-run it - a fresh dataset_version with no Batch row yet (a
    # script-generated dataset never run before) has nothing to check.
    existing_batch = session.query(Batch).filter_by(batch_id=batch_id).first()
    if existing_batch is not None:
        require_visible_batch(session, batch_id, user)
    existing = _RUNS.get(batch_id)
    if existing is not None and existing.running:
        raise HTTPException(status_code=409, detail=f"a run for {batch_id!r} is already in progress")
    state = _RunState(dataset_version=req.dataset_version)
    _RUNS[batch_id] = state
    thread = threading.Thread(target=_run_in_background, args=(req.dataset_version, batch_id, state), daemon=True)
    thread.start()
    return RunStatus(batch_id=batch_id, dataset_version=req.dataset_version, running=True, stage="RUNNING", processed=0, elapsed_seconds=0.0)


@router.get("/{batch_id}/status", response_model=RunStatus)
def run_status(batch_id: str, session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> RunStatus:
    if session.query(Batch).filter_by(batch_id=batch_id).first() is not None:
        require_visible_batch(session, batch_id, user)
    state = _RUNS.get(batch_id)
    if state is None:
        return RunStatus(batch_id=batch_id, dataset_version="", running=False, stage="QUEUED")
    with state.lock:
        running, error_message = state.running, state.error_message
        total, resolved, escalated, errors = state.total, state.resolved, state.escalated, state.errors
        elapsed = (state.finished_at or time.time()) - state.started_at
        dataset_version = state.dataset_version
    stage = "FAILED" if error_message else ("RUNNING" if running else "COMPLETED")
    # run_batch (app.orchestrator.batch_runner, unchanged) commits one
    # ReconciliationCase row per order as it works through the batch, so
    # counting them from a plain read here is a real, live measurement of
    # cases reached so far - not a fabricated percentage - without
    # reaching into orchestrator internals to get it. resolved/escalated
    # above only update once at the very end (run_batch returns a single
    # summary), so `processed` is the only true incremental signal
    # available for a run in progress.
    processed = (
        session.query(ReconciliationCase).filter_by(batch_id=batch_id).count()
        if running or resolved or escalated or errors else 0
    )
    return RunStatus(
        batch_id=batch_id, dataset_version=dataset_version, running=running,
        total=total, resolved=resolved, escalated=escalated,
        errors=errors, error_message=error_message,
        stage=stage, processed=processed, elapsed_seconds=elapsed,
    )


@router.get("/{batch_id}/events", response_model=list[AgentEventItem])
def batch_events(batch_id: str, after_id: int = Query(default=0), limit: int = Query(default=500, le=2000),
                  session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[AgentEventItem]:
    if session.query(Batch).filter_by(batch_id=batch_id).first() is not None:
        require_visible_batch(session, batch_id, user)
    case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
    if not case_ids:
        return []
    rows = (
        session.query(AgentEvent)
        .filter(AgentEvent.case_id.in_(case_ids), AgentEvent.id > after_id)
        .order_by(AgentEvent.id)
        .limit(limit)
        .all()
    )
    return serialize_events(rows)


@router.get("/{batch_id}/stream")
async def stream_events(
    batch_id: str, after_id: int = Query(default=0), session: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> StreamingResponse:
    if session.query(Batch).filter_by(batch_id=batch_id).first() is not None:
        require_visible_batch(session, batch_id, user)

    async def event_source():
        last_id = after_id
        idle_polls = 0
        while True:
            session = SessionLocal()
            try:
                case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
                rows = []
                if case_ids:
                    rows = (
                        session.query(AgentEvent)
                        .filter(AgentEvent.case_id.in_(case_ids), AgentEvent.id > last_id)
                        .order_by(AgentEvent.id)
                        .limit(200)
                        .all()
                    )
                state = _RUNS.get(batch_id)
                running = state.running if state else False
            finally:
                session.close()

            if rows:
                idle_polls = 0
                for e in rows:
                    last_id = e.id
                    item = AgentEventItem(
                        id=e.id, case_id=e.case_id, from_state=e.from_state.value if e.from_state else None,
                        to_state=e.to_state.value, tool=e.tool, input_summary=e.input_summary,
                        output_summary=e.output_summary, message=e.message, verifier_result=e.verifier_result,
                        created_at=e.created_at,
                    )
                    yield f"data: {item.model_dump_json()}\n\n"
            else:
                idle_polls += 1
                if not running and idle_polls >= 2:
                    yield "event: done\ndata: {}\n\n"
                    return
                yield ": heartbeat\n\n"

            await asyncio.sleep(0.6)

    return StreamingResponse(event_source(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })
