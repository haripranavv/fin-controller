"""Exceptions screen — PROJECT_SPEC.md section 17.5: case, amount,
severity, reason, evidence, AI proposal/confidence, verifier failure. The
verifier failure comes directly from the persisted AgentEvent that carried
the failing verifier_result (the transition INTO ESCALATED, or the VERIFY
-> DIVERGENCE_TRACE transition for cases that never reached a proposal) -
not recomputed, the actual result the real verifier produced."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.common import case_value_paisa, categorize_reason, require_visible_batch
from app.api.routes_auth import get_current_user
from app.api.schemas import ExceptionListItem, ExceptionListResponse
from app.db.session import get_db
from app.models.auth import User
from app.models.enums import CaseState
from app.models.financial import Order
from app.models.operational import AgentEvent, ExceptionRecord, Investigation, ReconciliationCase

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])


@router.get("", response_model=ExceptionListResponse)
def list_exceptions(batch_id: str = Query(...), session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ExceptionListResponse:
    require_visible_batch(session, batch_id, user)
    case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
    if not case_ids:
        return ExceptionListResponse(total=0, total_value_paisa=0, exceptions=[])

    exc_rows = session.query(ExceptionRecord).filter(ExceptionRecord.case_id.in_(case_ids)).order_by(ExceptionRecord.created_at.desc()).all()
    exc_case_ids = [e.case_id for e in exc_rows]
    cases_by_id = {c.case_id: c for c in session.query(ReconciliationCase).filter(ReconciliationCase.case_id.in_(exc_case_ids)).all()}
    anchor_ids = [c.anchor_id for c in cases_by_id.values()]
    orders_by_id = {o.order_id: o for o in session.query(Order).filter(Order.order_id.in_(anchor_ids)).all()} if anchor_ids else {}
    investigations = {i.case_id: i for i in session.query(Investigation).filter(Investigation.case_id.in_(exc_case_ids)).all()}

    # The verifier_result carried on the terminal AgentEvent for each case
    # (VERIFY -> ESCALATED for a failed-proposal escalation, or
    # DIVERGENCE_TRACE for an unresolved/no-known-cause escalation that
    # never reached a proposal, or NO_MATCH -> ESCALATED which carries none).
    last_events = {}
    if exc_case_ids:
        for e in session.query(AgentEvent).filter(AgentEvent.case_id.in_(exc_case_ids), AgentEvent.to_state == CaseState.ESCALATED).all():
            last_events[e.case_id] = e

    items: list[ExceptionListItem] = []
    for e in exc_rows:
        case = cases_by_id.get(e.case_id)
        order = orders_by_id.get(case.anchor_id) if case else None
        inv = investigations.get(e.case_id)
        terminal_event = last_events.get(e.case_id)
        _, stage_reached = categorize_reason(e.reason)
        items.append(ExceptionListItem(
            case_id=e.case_id, order_id=case.anchor_id if case else "", amount_paisa=case_value_paisa(order.amount_paisa if order else None),
            severity=e.severity.value, reason=e.reason, stage_reached=stage_reached,
            divergence_stage=inv.divergence_stage.value if (inv and inv.divergence_stage) else None,
            expected_amount_paisa=inv.expected_amount_paisa if inv else None,
            actual_amount_paisa=inv.actual_amount_paisa if inv else None,
            delta_paisa=inv.delta_paisa if inv else None,
            status=e.status,
            root_cause=inv.root_cause.value if (inv and inv.root_cause) else None,
            confidence=inv.confidence if inv else None,
            verifier_result=terminal_event.verifier_result if terminal_event else None,
            created_at=e.created_at,
        ))

    total_value = sum(i.amount_paisa for i in items)
    return ExceptionListResponse(total=len(items), total_value_paisa=total_value, exceptions=items)
