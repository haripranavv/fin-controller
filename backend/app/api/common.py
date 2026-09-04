"""Shared helpers for the API routers — no decision logic, just DB reads
and formatting shared across more than one route module."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.schemas import AgentEventItem
from app.models.auth import User
from app.models.enums import CaseState
from app.models.operational import AgentEvent, Batch, Investigation, ReconciliationCase


def outcome_of(state: CaseState | str) -> str:
    state = state.value if isinstance(state, CaseState) else state
    if state == CaseState.RESOLVED.value:
        return "RESOLVED"
    if state == CaseState.ESCALATED.value:
        return "ESCALATED"
    return "IN_PROGRESS"


# --- user isolation ("users cannot access other users' batches/cases") -------------
#
# A batch with user_id=NULL is "system-owned" (created by a script, or by
# any pre-existing run from before this stage) and stays visible to every
# authenticated user for backward compatibility - see app.models.
# operational.Batch's user_id column docstring. A batch with a real
# user_id is visible ONLY to that user. Every route below that resolves a
# batch_id or a case_id now goes through one of these two helpers instead
# of a bare query, so isolation can't be silently skipped in one route
# and not another.


def batch_visibility_filter(user: User):
    return or_(Batch.user_id == user.id, Batch.user_id.is_(None))


def require_visible_batch(session: Session, batch_id: str, user: User) -> Batch:
    batch = session.query(Batch).filter_by(batch_id=batch_id).first()
    if batch is None or not (batch.user_id is None or batch.user_id == user.id):
        # 404, not 403 - never confirm to a caller that a batch belonging
        # to someone else even exists.
        raise HTTPException(status_code=404, detail=f"batch {batch_id!r} not found")
    return batch


def get_case_or_404(session: Session, case_id: str, user: User) -> ReconciliationCase:
    case = session.query(ReconciliationCase).filter_by(case_id=case_id).first()
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id!r} not found")
    require_visible_batch(session, case.batch_id, user)  # 404s if the case's batch isn't this user's
    return case


def ai_invoked_case_ids(session: Session, case_ids: list[str]) -> set[str]:
    """Cases whose AgentEvent trail actually reached ROOT_CAUSE_INVESTIGATE
    - the same architectural signal app.orchestrator.case_runner.run_case
    itself uses to decide whether to emit that event (see its
    `ai_was_invoked` computation), read back rather than re-derived."""
    if not case_ids:
        return set()
    rows = (
        session.query(AgentEvent.case_id)
        .filter(AgentEvent.case_id.in_(case_ids), AgentEvent.to_state == CaseState.ROOT_CAUSE_INVESTIGATE)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def resolved_via_of(outcome: str, has_investigation: bool, ai_invoked: bool) -> str | None:
    if outcome != "RESOLVED":
        return None
    if not has_investigation:
        return "clean"
    return "ai" if ai_invoked else "deterministic"


# Every ESCALATED case's ExceptionRecord.reason is one of exactly these
# prefixes/substrings, written by app.orchestrator.case_runner.run_case
# (unchanged) - this is display-only text categorization of an already-
# persisted string, not new decision logic. Order matters: checked
# top-to-bottom, first match wins.
_REASON_CATEGORIES: list[tuple[str, str, str]] = [
    # (substring to match, short human category, stage_reached)
    ("no_match:", "No confident settlement match found", "no_match"),
    ("unresolved -", "Missing bank evidence to trace the divergence", "divergence_trace"),
    ("no known deterministic cause", "No known cause for the divergence", "root_cause_investigate"),
    ("failed verification", "Proposed cause failed verifier checks", "verify"),
]


def categorize_reason(reason: str) -> tuple[str, str]:
    """Returns (short human category, stage_reached) for an ExceptionRecord
    reason string. Falls back to a truncated echo of the reason itself if
    it doesn't match a known pattern (should not happen given the closed
    set of reasons case_runner.py writes, but never raises on an
    unexpected string)."""
    for needle, category, stage in _REASON_CATEGORIES:
        if needle in reason:
            return category, stage
    return (reason[:60] + ("…" if len(reason) > 60 else "")), "unknown"


def finding_for(outcome: str, resolved_via: str | None, root_cause: str | None, reason: str | None) -> str:
    """One-line, human-readable summary of what happened to a case - the
    Reconciliation table's "Finding" column and a building block for
    other screens' narrative text."""
    if outcome == "RESOLVED":
        if resolved_via == "clean":
            return "Chain reconciled exactly - no divergence"
        cause = (root_cause or "cause").replace("_", " ")
        via = "AI investigator" if resolved_via == "ai" else "deterministic rule"
        return f"Resolved: {cause} ({via}, verifier-confirmed)"
    if outcome == "ESCALATED":
        category, _ = categorize_reason(reason or "")
        return category
    return "In progress"


def investigations_by_case(session: Session, case_ids: list[str]) -> dict[str, Investigation]:
    if not case_ids:
        return {}
    rows = session.query(Investigation).filter(Investigation.case_id.in_(case_ids)).all()
    return {r.case_id: r for r in rows}


def case_value_paisa(order_amount_paisa: int | None) -> int:
    return abs(order_amount_paisa) if order_amount_paisa is not None else 0


def serialize_events(rows: list[AgentEvent]) -> list[AgentEventItem]:
    return [
        AgentEventItem(
            id=e.id, case_id=e.case_id,
            from_state=e.from_state.value if e.from_state else None,
            to_state=e.to_state.value, tool=e.tool,
            input_summary=e.input_summary, output_summary=e.output_summary,
            message=e.message, verifier_result=e.verifier_result, created_at=e.created_at,
        )
        for e in rows
    ]


def batch_throughput(session: Session, case_ids: list[str]) -> float | None:
    """cases / (span between the batch's first and last AgentEvent) —
    the real wall-clock span the persisted events were written across."""
    if not case_ids:
        return None
    lo, hi = session.query(func.min(AgentEvent.created_at), func.max(AgentEvent.created_at)).filter(
        AgentEvent.case_id.in_(case_ids)
    ).first()
    if lo is None or hi is None or lo == hi:
        return None
    span_seconds = (hi - lo).total_seconds()
    if span_seconds <= 0:
        return None
    return len(case_ids) / span_seconds
