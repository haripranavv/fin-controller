"""GET /api/overview — PROJECT_SPEC.md section 17.1: total records/cases,
resolved, escalated, match rate, Rs affected, throughput. Live-computed
from persisted operational state (never ground truth) for the selected
batch, plus the most recent offline EvaluationRun row per mode (that table
lives in the main operational DB, not the ground_truth schema — it's the
already-scored OUTPUT of an offline comparison, safe to surface; the
comparison itself never runs here)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from collections import Counter

from app.api.common import (
    ai_invoked_case_ids, batch_throughput, batch_visibility_filter, categorize_reason,
    case_value_paisa, investigations_by_case, serialize_events,
)
from app.api.routes_auth import get_current_user
from app.api.schemas import AttentionCase, EscalationReasonCount, EvaluationSummary, OverviewResponse
from app.db.session import get_db
from app.models.auth import User
from app.models.enums import CaseState
from app.models.financial import Order
from app.models.operational import AgentEvent, Batch, EvaluationRun, ExceptionRecord, ReconciliationCase

router = APIRouter(prefix="/api/overview", tags=["overview"])


@router.get("", response_model=OverviewResponse)
def overview(
    batch_id: str | None = Query(default=None), session: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> OverviewResponse:
    batch = (
        session.query(Batch).filter_by(batch_id=batch_id).filter(batch_visibility_filter(user)).first()
        if batch_id else session.query(Batch).filter(batch_visibility_filter(user)).order_by(Batch.created_at.desc()).first()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="no batch found - import data or run scripts/run_orchestrator.py first")

    cases = session.query(ReconciliationCase).filter_by(batch_id=batch.batch_id).all()
    case_ids = [c.case_id for c in cases]
    resolved_ids = [c.case_id for c in cases if c.state == CaseState.RESOLVED]
    escalated_count = sum(1 for c in cases if c.state == CaseState.ESCALATED)

    anchor_ids = [c.anchor_id for c in cases]
    orders_by_id = {}
    if anchor_ids:
        orders_by_id = {o.order_id: o for o in session.query(Order).filter(Order.order_id.in_(anchor_ids)).all()}

    total_value = sum(abs(orders_by_id[c.anchor_id].amount_paisa) for c in cases if c.anchor_id in orders_by_id)
    resolved_value = sum(
        abs(orders_by_id[c.anchor_id].amount_paisa) for c in cases
        if c.anchor_id in orders_by_id and c.case_id in resolved_ids
    )

    exc_count, exc_value = session.query(func.count(ExceptionRecord.id), func.coalesce(func.sum(ExceptionRecord.amount_paisa), 0)).filter(
        ExceptionRecord.case_id.in_(case_ids)
    ).first() if case_ids else (0, 0)

    # Operational summary: how RESOLVED cases actually got resolved, and
    # what ESCALATED cases are stuck on - "what did the controller do on
    # this run", not a ground-truth-scored claim.
    investigations = investigations_by_case(session, resolved_ids)
    ai_invoked = ai_invoked_case_ids(session, resolved_ids)
    clean_resolved = deterministic_resolved = ai_resolved = 0
    for cid in resolved_ids:
        if cid not in investigations:
            clean_resolved += 1
        elif cid in ai_invoked:
            ai_resolved += 1
        else:
            deterministic_resolved += 1

    escalated_ids = [c.case_id for c in cases if c.state == CaseState.ESCALATED]
    exc_reasons = (
        session.query(ExceptionRecord.case_id, ExceptionRecord.reason, ExceptionRecord.amount_paisa)
        .filter(ExceptionRecord.case_id.in_(escalated_ids)).all()
        if escalated_ids else []
    )
    reason_counts: Counter[str] = Counter()
    reason_values: dict[str, int] = {}
    for _cid, reason, amount in exc_reasons:
        category, _stage = categorize_reason(reason)
        reason_counts[category] += 1
        reason_values[category] = reason_values.get(category, 0) + amount
    top_escalation_reasons = [
        EscalationReasonCount(reason_category=cat, count=n, value_paisa=reason_values.get(cat, 0))
        for cat, n in reason_counts.most_common(5)
    ]

    # Control Center: "what needs attention" - the escalated cases with
    # the most value at stake, and one suggested case to open first.
    escalated_cases_full = [c for c in cases if c.state == CaseState.ESCALATED]
    exc_by_case = {
        e.case_id: e for e in session.query(ExceptionRecord).filter(ExceptionRecord.case_id.in_(escalated_ids)).all()
    } if escalated_ids else {}
    attention: list[AttentionCase] = []
    for c in escalated_cases_full:
        exc = exc_by_case.get(c.case_id)
        order = orders_by_id.get(c.anchor_id)
        category, _stage = categorize_reason(exc.reason if exc else "")
        attention.append(AttentionCase(
            case_id=c.case_id, order_id=c.anchor_id, amount_paisa=case_value_paisa(order.amount_paisa if order else None),
            severity=exc.severity.value if exc else "medium", reason_category=category,
        ))
    attention.sort(key=lambda a: a.amount_paisa, reverse=True)
    attention = attention[:8]
    highlighted_case_id = attention[0].case_id if attention else None

    recent_event_rows = (
        session.query(AgentEvent).filter(AgentEvent.case_id.in_(case_ids)).order_by(AgentEvent.id.desc()).limit(15).all()
        if case_ids else []
    )

    eval_rows = (
        session.query(EvaluationRun)
        .filter(EvaluationRun.dataset_version == batch.dataset_version)
        .order_by(EvaluationRun.created_at.desc())
        .all()
    )
    seen_modes: set = set()
    last_eval: list[EvaluationSummary] = []
    for r in eval_rows:
        if r.mode in seen_modes:
            continue
        seen_modes.add(r.mode)
        last_eval.append(EvaluationSummary(
            mode=r.mode.value, records_processed=r.records_processed, match_rate=r.match_rate,
            match_rate_by_value=r.match_rate_by_value, precision=r.precision, recall=r.recall,
            false_match_rate=r.false_match_rate, exception_count=r.exception_count,
            exception_value_paisa=r.exception_value_paisa, ai_assisted_resolution_rate=r.ai_assisted_resolution_rate,
            throughput=r.throughput, created_at=r.created_at,
        ))

    total = len(cases)
    return OverviewResponse(
        batch_id=batch.batch_id, dataset_version=batch.dataset_version,
        total_cases=total, resolved=len(resolved_ids), escalated=escalated_count,
        in_progress=total - len(resolved_ids) - escalated_count,
        resolution_rate=(len(resolved_ids) / total) if total else 0.0,
        monetary_resolution_rate=(resolved_value / total_value) if total_value else 0.0,
        total_value_paisa=total_value, resolved_value_paisa=resolved_value,
        exception_count=exc_count, exception_value_paisa=exc_value,
        throughput_cases_per_sec=batch_throughput(session, case_ids),
        clean_resolved=clean_resolved, deterministic_resolved=deterministic_resolved, ai_resolved=ai_resolved,
        top_escalation_reasons=top_escalation_reasons,
        attention_cases=attention, highlighted_case_id=highlighted_case_id,
        recent_events=serialize_events(recent_event_rows),
        last_evaluation=last_eval,
    )
