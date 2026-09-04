"""Reconciliation (dense filterable case table), Record Detail (full case
dossier), and Investigation (financial chain / first divergence / evidence
/ root-cause proposal / verifier result) screens - PROJECT_SPEC.md
section 17.2, 17.4, 17.6."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.case_reconstruct import reconstruct_case
from app.api.common import (
    ai_invoked_case_ids, case_value_paisa, finding_for, get_case_or_404, investigations_by_case,
    outcome_of, require_visible_batch, resolved_via_of, serialize_events,
)
from app.api.routes_auth import get_current_user
from app.api.schemas import (
    CaseDetail, CaseListItem, CaseListResponse, ExceptionRow, FinancialRecord, InvestigationDetail,
    InvestigationRow, MatchItem, PrivacyBoundary, StageDTO,
)
from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.db.session import get_db
from app.divergence.tracer import trace_chain
from app.models.auth import User
from app.models.enums import CaseState
from app.models.financial import Order
from app.models.operational import AgentEvent, ExceptionRecord, Investigation, Match, ReconciliationCase
from app.rootcause.evidence import build_evidence

router = APIRouter(prefix="/api/cases", tags=["cases"])


def _investigation_row(inv) -> InvestigationRow | None:
    if inv is None:
        return None
    return InvestigationRow(
        divergence_stage=inv.divergence_stage.value if inv.divergence_stage else None,
        expected_amount_paisa=inv.expected_amount_paisa, actual_amount_paisa=inv.actual_amount_paisa,
        delta_paisa=inv.delta_paisa, root_cause=inv.root_cause.value if inv.root_cause else None,
        confidence=inv.confidence, status=inv.status, created_at=inv.created_at,
    )


@router.get("", response_model=CaseListResponse)
def list_cases(
    batch_id: str = Query(...),
    state: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaseListResponse:
    require_visible_batch(session, batch_id, user)
    query = session.query(ReconciliationCase).filter_by(batch_id=batch_id)
    if state:
        query = query.filter(ReconciliationCase.state == CaseState(state))
    if q:
        query = query.filter(ReconciliationCase.anchor_id.ilike(f"%{q}%"))

    total = query.count()
    cases = query.order_by(ReconciliationCase.updated_at.desc()).offset(offset).limit(limit).all()
    case_ids = [c.case_id for c in cases]
    anchor_ids = [c.anchor_id for c in cases]

    orders_by_id = {o.order_id: o for o in session.query(Order).filter(Order.order_id.in_(anchor_ids)).all()} if anchor_ids else {}
    investigations = investigations_by_case(session, case_ids)
    ai_invoked = ai_invoked_case_ids(session, case_ids)
    exceptions = {
        e.case_id: e for e in session.query(ExceptionRecord).filter(ExceptionRecord.case_id.in_(case_ids)).all()
    } if case_ids else {}

    items: list[CaseListItem] = []
    for c in cases:
        outcome = outcome_of(c.state)
        inv = investigations.get(c.case_id)
        exc = exceptions.get(c.case_id)
        order = orders_by_id.get(c.anchor_id)
        root_cause = inv.root_cause.value if (inv and inv.root_cause) else None
        resolved_via = resolved_via_of(outcome, inv is not None, c.case_id in ai_invoked)
        items.append(CaseListItem(
            case_id=c.case_id, order_id=c.anchor_id, state=c.state.value, outcome=outcome,
            amount_paisa=case_value_paisa(order.amount_paisa if order else None),
            root_cause=root_cause, resolved_via=resolved_via,
            severity=exc.severity.value if exc else None,
            finding=finding_for(outcome, resolved_via, root_cause, exc.reason if exc else None),
            created_at=c.created_at, updated_at=c.updated_at,
        ))
    return CaseListResponse(total=total, cases=items)


def _financial_record(record_type: str, record_id: str, amount_paisa: int, **detail) -> FinancialRecord:
    return FinancialRecord(record_type=record_type, record_id=record_id, amount_paisa=amount_paisa, detail=detail)


def _order_fr(o: GenOrder) -> FinancialRecord:
    return _financial_record("order", o.order_id, o.amount_paisa, currency=o.currency, status=o.status, created_at=str(o.created_at))


def _payment_fr(p: GenPayment) -> FinancialRecord:
    return _financial_record("payment", p.payment_id, p.amount_paisa, fee_paisa=p.fee_paisa,
                              tax_on_fee_paisa=p.tax_on_fee_paisa, method=p.method, status=p.status,
                              narration=p.narration, created_at=str(p.created_at))


def _refund_fr(r: GenRefund) -> FinancialRecord:
    return _financial_record("refund", r.refund_id, r.amount_paisa, reason_code=r.reason_code,
                              narration=r.narration, created_at=str(r.created_at))


def _settlement_fr(s: GenSettlement) -> FinancialRecord:
    return _financial_record("settlement", s.settlement_id, s.settled_amount_paisa, fee_deducted_paisa=s.fee_deducted_paisa,
                              period_start=str(s.period_start), period_end=str(s.period_end))


def _bank_fr(b: GenBankTransaction) -> FinancialRecord:
    return _financial_record("bank_transaction", b.bank_txn_id, b.amount_paisa, value_date=str(b.value_date),
                              utr_ref=b.utr_ref, narration=b.narration)


@router.get("/{case_id}", response_model=CaseDetail)
def case_detail(case_id: str, session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> CaseDetail:
    case = get_case_or_404(session, case_id, user)
    rc = reconstruct_case(session, case)
    matches = session.query(Match).filter_by(case_id=case_id).all()
    inv = session.query(Investigation).filter_by(case_id=case_id).first()
    exc = session.query(ExceptionRecord).filter_by(case_id=case_id).first()
    events = session.query(AgentEvent).filter_by(case_id=case_id).order_by(AgentEvent.id).all()

    return CaseDetail(
        case_id=case.case_id, order_id=case.anchor_id, batch_id=case.batch_id, state=case.state.value,
        outcome=outcome_of(case.state), created_at=case.created_at, updated_at=case.updated_at,
        order=_order_fr(rc.order) if rc.order else None,
        payments=[_payment_fr(p) for p in rc.payments],
        refunds=[_refund_fr(r) for r in rc.refunds],
        settlement=_settlement_fr(rc.settlement) if rc.settlement else None,
        bank_txns=[_bank_fr(b) for b in rc.bank_txns],
        matches=[MatchItem(source_type=m.source_type.value, source_id=m.source_id, target_type=m.target_type.value,
                            target_id=m.target_id, method=m.method.value, score=m.score, accepted=m.accepted) for m in matches],
        investigation=_investigation_row(inv),
        exception=ExceptionRow(reason=exc.reason, severity=exc.severity.value, amount_paisa=exc.amount_paisa,
                                status=exc.status, created_at=exc.created_at) if exc else None,
        events=serialize_events(events),
    )


def _stage_timestamp(stage_name: str, rc):
    if stage_name == "order":
        return rc.order.created_at if rc.order else None
    if stage_name == "payment":
        payments = rc.payments or []
        return max((p.created_at for p in payments), default=None)
    if stage_name == "refund":
        refunds = rc.refunds or []
        return max((r.created_at for r in refunds), default=None) if refunds else None
    if stage_name == "settlement":
        return rc.settlement.created_at if rc.settlement else None
    if stage_name == "bank":
        bank_txns = rc.bank_txns or []
        return max((b.value_date for b in bank_txns), default=None) if bank_txns else None
    return None


def _stage_dto(stage, rc, is_first: bool) -> StageDTO:
    return StageDTO(
        stage=stage.stage, expected_paisa=stage.expected_paisa, actual_paisa=stage.actual_paisa,
        delta_paisa=stage.delta_paisa, consistent=stage.consistent, note=stage.note,
        evidence=stage.evidence, is_first_divergence=is_first, timestamp=_stage_timestamp(stage.stage, rc),
    )


@router.get("/{case_id}/investigation", response_model=InvestigationDetail)
def case_investigation(case_id: str, session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> InvestigationDetail:
    case = get_case_or_404(session, case_id, user)
    inv = session.query(Investigation).filter_by(case_id=case_id).first()
    events = session.query(AgentEvent).filter_by(case_id=case_id).order_by(AgentEvent.id).all()
    order_row = session.query(Order).filter_by(order_id=case.anchor_id).first()
    outcome = outcome_of(case.state)
    ai_was_invoked = case_id in ai_invoked_case_ids(session, [case_id])
    resolved_via = resolved_via_of(outcome, inv is not None, ai_was_invoked)

    rc = reconstruct_case(session, case)
    chain: list[StageDTO] = []
    downstream: list[StageDTO] = []
    trace_status: str | None = None
    first_divergence_stage: str | None = None
    privacy: PrivacyBoundary | None = None

    if rc.order is not None and rc.settlement is not None:
        trace = trace_chain(
            rc.order, rc.payments, rc.refunds, rc.settlement, rc.bank_txns,
            settlement_group_payments=rc.settlement_group_payments or rc.payments,
            settlement_group_refunds=rc.settlement_group_refunds or rc.refunds,
        )
        trace_status = trace.status
        first_divergence_stage = trace.first_divergence_stage
        for stage in trace.stages:
            is_first = trace.first_divergence is not None and stage is trace.first_divergence
            chain.append(_stage_dto(stage, rc, is_first))
        downstream = [_stage_dto(stage, rc, False) for stage in trace.downstream_impact]

        if ai_was_invoked:
            # The EXACT payload app.rootcause.investigator.investigate_root_cause
            # sent to Gemini for this case - app.rootcause.evidence.
            # build_evidence, UNCHANGED, re-run on the same reconstructed
            # group_refunds/bank_txns case_runner.py fed it. Not a claim
            # about what was sent - this literally is what was sent (same
            # pure function, same inputs, same deterministic output).
            group_refunds = rc.settlement_group_refunds or rc.refunds
            evidence_payload = build_evidence(group_refunds, rc.bank_txns)
            privacy = PrivacyBoundary(
                evidence_sent=evidence_payload,
                raw_files_sent=False, ground_truth_sent=False, unnecessary_pii_sent=False, structured_evidence_sent=True,
            )

    return InvestigationDetail(
        case_id=case.case_id, order_id=case.anchor_id, state=case.state.value, outcome=outcome,
        amount_paisa=case_value_paisa(order_row.amount_paisa if order_row else None),
        chain_available=bool(chain), chain=chain, first_divergence_stage=first_divergence_stage,
        trace_status=trace_status, downstream_impact=downstream,
        investigation=_investigation_row(inv), resolved_via=resolved_via, ai_was_invoked=ai_was_invoked,
        privacy=privacy, events=serialize_events(events),
    )
