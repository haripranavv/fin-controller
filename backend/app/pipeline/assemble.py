"""Assembles CaseInputs for one order from a batch's raw records plus the
matcher's own accepted output — mirroring what a real orchestrator would
do after MATCH_ATTEMPT, before handing a case to VERIFY. Never touches
ground truth.
"""
from __future__ import annotations

from app.datagen.models import GeneratedBatch
from app.matcher.reconciler import MatcherRunResult
from app.pipeline.types import CaseInputs


def assemble_case_inputs(batch: GeneratedBatch, result: MatcherRunResult, order_id: str) -> CaseInputs:
    order = next(o for o in batch.orders if o.order_id == order_id)
    payments = [p for p in batch.payments if p.order_id == order_id]
    payment_ids = {p.payment_id for p in payments}
    refunds = [r for r in batch.refunds if r.payment_id in payment_ids]

    accepted_settlement = [m for m in result.settlement_payment if m.accepted]
    settlement_match = next((m for m in accepted_settlement if m.source_id in payment_ids), None)

    if settlement_match is None:
        return CaseInputs(
            order=order, payments=payments, refunds=refunds,
            settlement=None, settlement_match=None,
        )

    settlement_id = settlement_match.target_id
    settlement = next(s for s in batch.settlements if s.settlement_id == settlement_id)

    group_payment_ids = {m.source_id for m in accepted_settlement if m.target_id == settlement_id}
    group_payments = [p for p in batch.payments if p.payment_id in group_payment_ids]
    group_refunds = [r for r in batch.refunds if r.payment_id in group_payment_ids]

    bank_matches = [m for m in result.settlement_bank if m.accepted and m.source_id == settlement_id]
    bank_ids = {m.target_id for m in bank_matches}
    bank_txns = [b for b in batch.bank_transactions if b.bank_txn_id in bank_ids]

    return CaseInputs(
        order=order, payments=payments, refunds=refunds,
        settlement=settlement, settlement_match=settlement_match,
        settlement_group_payments=group_payments, settlement_group_refunds=group_refunds,
        bank_txns=bank_txns, bank_matches=bank_matches,
    )
