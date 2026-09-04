#!/usr/bin/env python
"""Runs the real agent orchestrator (app.orchestrator) over a persisted
dataset, wiring app.matcher / app.verifier / app.divergence /
app.pipeline.known_causes / app.rootcause together into PROJECT_SPEC.md
section 6's bounded state machine — with real persistence: a
ReconciliationCase, Match, Investigation, and (on escalation)
ExceptionRecord row per case, plus one AgentEvent per state transition.

Uses the REAL Anthropic API if ANTHROPIC_API_KEY is set, otherwise a
rule-based stand-in client (same reasoning validated in
scripts/run_rootcause_eval.py), clearly labeled either way.

Idempotent: refuses to re-run over a batch that already has
ReconciliationCase rows unless --overwrite is passed, in which case prior
orchestration rows for this batch (AgentEvent, Match, Investigation,
ExceptionRecord, ReconciliationCase — FK-safe order) are purged first.
Financial records and ground truth are untouched either way.

Usage (run from backend/, with the venv active, Postgres up):
    python scripts/run_orchestrator.py --dataset-version dev-v1
    python scripts/run_orchestrator.py --dataset-version dev-v1 --overwrite
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.datagen import catalog  # noqa: E402
from app.datagen.models import GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.models.operational import AgentEvent, ExceptionRecord, Investigation, Match, ReconciliationCase  # noqa: E402
from app.orchestrator.batch_runner import run_batch  # noqa: E402
from app.rootcause.client import AnthropicRootCauseClient, GeminiRootCauseClient, RootCauseLLMClient  # noqa: E402

_TOKEN_TO_MERCHANT = {catalog.token(name): name for _mid, name in catalog.MERCHANTS}
_DECLINE = json.dumps({"root_cause": "unknown", "supporting_evidence": [], "confidence": 0.20, "explanation": "no supporting evidence found"})


class _ReferenceStandInClient:
    """The same reasoning validated in scripts/run_rootcause_eval.py and
    scripts/run_rootcause_validation.py, wrapped as a RootCauseLLMClient.
    Never reads ground truth."""

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
        return _DECLINE


def purge_orchestration(session, batch_id: str) -> None:
    case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
    if not case_ids:
        return
    for model in (AgentEvent, Match, Investigation, ExceptionRecord):
        session.execute(delete(model).where(model.case_id.in_(case_ids)))
    session.execute(delete(ReconciliationCase).where(ReconciliationCase.case_id.in_(case_ids)))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--tolerance-paisa", type=int, default=0)
    args = parser.parse_args()
    dv = args.dataset_version

    if settings.gemini_api_key:
        client: RootCauseLLMClient = GeminiRootCauseClient(settings.gemini_api_key, settings.gemini_model)
        mode = f"REAL Gemini API ({settings.gemini_model})"
    elif settings.anthropic_api_key:
        client = AnthropicRootCauseClient(settings.anthropic_api_key, settings.anthropic_model)
        mode = f"REAL Anthropic API ({settings.anthropic_model})"
    else:
        client = _ReferenceStandInClient()
        mode = "SIMULATED - no GEMINI_API_KEY/ANTHROPIC_API_KEY set, using a rule-based stand-in, NOT a real LLM call"

    session = SessionLocal()
    try:
        orders, payments, refunds, settlements, bank_txns = load_dataset(session, dv)
        batch_id = f"batch_{dv}"

        existing = session.query(ReconciliationCase).filter_by(batch_id=batch_id).count()
        if existing:
            if not args.overwrite:
                print(f"{existing} case(s) already exist for batch {batch_id!r}. Pass --overwrite to re-run.")
                return
            print(f"--overwrite: purging {existing} existing case(s) and their audit trail for {batch_id!r}...")
            purge_orchestration(session, batch_id)

        batch = GeneratedBatch(batch_id=batch_id, dataset_version=dv, seed=0,
                                orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)

        print(f"=== Milestone 9 orchestrator run: {dv} ===")
        print(f"AI mode: {mode}")
        print(f"orders={len(orders)}  payments={len(payments)}  settlements={len(settlements)}  bank_txns={len(bank_txns)}")
        print()

        summary = run_batch(session, batch, client, tolerance_paisa=args.tolerance_paisa)

        print(f"total:     {summary.total}")
        print(f"resolved:  {summary.resolved} ({summary.resolved / summary.total:.1%})")
        print(f"escalated: {summary.escalated} ({summary.escalated / summary.total:.1%})")
        print(f"errors:    {len(summary.errors)}")
        for order_id, msg in summary.errors:
            print(f"  ERROR {order_id}: {msg}")
        print()

        case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
        event_count = session.query(AgentEvent).filter(AgentEvent.case_id.in_(case_ids)).count()
        match_count = session.query(Match).filter(Match.case_id.in_(case_ids)).count()
        investigation_count = session.query(Investigation).filter(Investigation.case_id.in_(case_ids)).count()
        exception_count = session.query(ExceptionRecord).filter(ExceptionRecord.case_id.in_(case_ids)).count()
        print(f"AgentEvent rows (this batch): {event_count}")
        print(f"Match rows (this batch): {match_count}")
        print(f"Investigation rows (this batch): {investigation_count}")
        print(f"ExceptionRecord rows (this batch): {exception_count}")
        print()

        # Ground truth used only for reporting, read-only, never for
        # matching/deciding — same exception every other scripts/run_*.py
        # report uses.
        gt_session = GroundTruthSessionLocal()
        try:
            gt_rows = gt_session.query(GroundTruth).filter(GroundTruth.record_id.like(f"%_{dv}_%")).all()
        finally:
            gt_session.close()
        gt_by_order = {g.record_id: g for g in gt_rows}

        by_cause: dict[str, Counter] = {}
        for c in summary.cases:
            gt = gt_by_order.get(c.order_id)
            cause = gt.true_root_cause if (gt and gt.true_root_cause) else "clean"
            by_cause.setdefault(cause, Counter())[c.outcome] += 1

        print("Outcome by true root cause (ground truth, comparison only):")
        for cause, counter in sorted(by_cause.items()):
            n = sum(counter.values())
            print(f"  {cause:32s} RESOLVED={counter['RESOLVED']:4d}  ESCALATED={counter['ESCALATED']:4d}  (n={n})")
    finally:
        session.close()


if __name__ == "__main__":
    main()
