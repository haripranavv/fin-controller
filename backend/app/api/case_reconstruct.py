"""Rebuilds one case's financial records (as app.datagen.models's Gen*
dataclasses — the same types app.divergence.tracer already consumes) from
already-persisted DB rows: the case's own Match rows for what was matched,
plus the underlying Order/Payment/Refund/Settlement/BankTransaction tables
for the actual figures. Used only to feed app.divergence.tracer.trace_chain
(UNCHANGED) for display, since only Investigation's single first-divergence
summary is persisted, not the full per-stage breakdown.

This is NOT the matcher run again — it does not decide anything. It reads
back what the matcher already decided (via the persisted Match rows) and
reconstructs the same shape of input trace_chain took the first time, so
re-running it here is guaranteed to reproduce the same output. Never reads
ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.models.enums import RecordType
from app.models.financial import BankTransaction, Order, Payment, Refund, Settlement
from app.models.operational import Match, ReconciliationCase


@dataclass
class ReconstructedCase:
    order: GenOrder | None
    payments: list[GenPayment]
    refunds: list[GenRefund]
    settlement: GenSettlement | None
    settlement_match_score: float | None
    settlement_match_method: str | None
    settlement_group_payments: list[GenPayment]
    settlement_group_refunds: list[GenRefund]
    bank_txns: list[GenBankTransaction]


def _to_gen_order(row: Order) -> GenOrder:
    return GenOrder(row.order_id, row.merchant_id, row.amount_paisa, row.currency, row.status, row.created_at)


def _to_gen_payment(row: Payment) -> GenPayment:
    return GenPayment(row.payment_id, row.order_id, row.amount_paisa, row.fee_paisa, row.tax_on_fee_paisa,
                       row.method, row.status, row.narration, row.created_at)


def _to_gen_refund(row: Refund) -> GenRefund:
    return GenRefund(row.refund_id, row.payment_id, row.amount_paisa, row.reason_code, row.narration, row.created_at)


def _to_gen_settlement(row: Settlement) -> GenSettlement:
    return GenSettlement(row.settlement_id, row.merchant_id, row.settled_amount_paisa, row.fee_deducted_paisa,
                          row.period_start, row.period_end, row.created_at)


def _to_gen_bank(row: BankTransaction) -> GenBankTransaction:
    return GenBankTransaction(row.bank_txn_id, row.amount_paisa, row.value_date, row.utr_ref, row.narration)


def reconstruct_case(session: Session, case: ReconciliationCase) -> ReconstructedCase:
    order_row = session.query(Order).filter_by(order_id=case.anchor_id).first()
    order = _to_gen_order(order_row) if order_row else None

    payment_rows = session.query(Payment).filter_by(order_id=case.anchor_id).all()
    payments = [_to_gen_payment(p) for p in payment_rows]
    payment_ids = {p.payment_id for p in payment_rows}

    refund_rows = session.query(Refund).filter(Refund.payment_id.in_(payment_ids)).all() if payment_ids else []
    refunds = [_to_gen_refund(r) for r in refund_rows]

    settlement_match = (
        session.query(Match)
        .filter_by(case_id=case.case_id, target_type=RecordType.SETTLEMENT, accepted=True)
        .first()
    )
    if settlement_match is None:
        return ReconstructedCase(order, payments, refunds, None, None, None, [], [], [])

    settlement_row = session.query(Settlement).filter_by(settlement_id=settlement_match.target_id).first()
    settlement = _to_gen_settlement(settlement_row) if settlement_row else None

    # Every case in the same settlement group persisted its own copy of
    # the accepted settlement-payment Match rows (see case_runner.py's
    # _persist_case_matches, called once per case with that case's own
    # inputs) — so querying Match by target_id across ALL cases (not
    # filtered to this case_id) recovers the full group, mirroring
    # app.pipeline.assemble.assemble_case_inputs's own group_payment_ids
    # computation exactly.
    group_matches = (
        session.query(Match)
        .filter_by(target_type=RecordType.SETTLEMENT, target_id=settlement_match.target_id, accepted=True)
        .all()
    )
    group_payment_ids = {m.source_id for m in group_matches}
    group_payment_rows = session.query(Payment).filter(Payment.payment_id.in_(group_payment_ids)).all() if group_payment_ids else []
    group_payments = [_to_gen_payment(p) for p in group_payment_rows]
    group_refund_rows = (
        session.query(Refund).filter(Refund.payment_id.in_(group_payment_ids)).all() if group_payment_ids else []
    )
    group_refunds = [_to_gen_refund(r) for r in group_refund_rows]

    bank_matches = (
        session.query(Match)
        .filter_by(case_id=case.case_id, source_type=RecordType.SETTLEMENT, target_type=RecordType.BANK_TRANSACTION, accepted=True)
        .all()
    )
    bank_ids = {m.target_id for m in bank_matches}
    bank_rows = session.query(BankTransaction).filter(BankTransaction.bank_txn_id.in_(bank_ids)).all() if bank_ids else []
    bank_txns = [_to_gen_bank(b) for b in bank_rows]

    return ReconstructedCase(
        order=order, payments=payments, refunds=refunds, settlement=settlement,
        settlement_match_score=settlement_match.score, settlement_match_method=settlement_match.method.value,
        settlement_group_payments=group_payments or payments,
        settlement_group_refunds=group_refunds or refunds,
        bank_txns=bank_txns,
    )
