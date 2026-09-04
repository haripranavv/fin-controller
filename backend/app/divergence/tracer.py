"""The divergence_tracer tool (PROJECT_SPEC.md section 7), implementing
section 12's first-divergence engine.

Deterministic chain walk: Order -> Payment -> Refund(s) -> Settlement ->
Bank. At each stage, compute expected/actual/delta using section 12's own
formulas, CHAINED — each stage's "expected" derives from the PREVIOUS
stage's ACTUAL, exactly as literally stated ("Bank expected =
Settlement.settled_amount" uses the settlement's own recorded/declared
value, not a theoretical corrected one). The first stage where
|expected - actual| exceeds tolerance is the FIRST POINT OF DIVERGENCE;
every stage after it is "downstream impact". Because the formulas already
chain off actual values (not the original correct expectation), a
downstream stage's OWN delta naturally represents any NEW divergence
introduced at that stage — it does not just re-report the upstream
problem. That is the whole mechanism behind "downstream impact" here;
no separate "corrected projection" pass is needed.

Called on an already-MATCHED chain (an accepted settlement match, and
whatever payments/refunds/bank transactions the matcher accepted for it —
see PROJECT_SPEC.md section 6: DIVERGENCE_TRACE is reached from
MATCHED -> VERIFY -> FAIL, not from NO_MATCH). Deterministic only: no AI,
no root-cause inference.
"""
from __future__ import annotations

from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.divergence.types import DivergenceTrace, StageResult
from app.models.enums import DivergenceStage

DEFAULT_TOLERANCE_PAISA = 0


def trace_chain(
    order: GenOrder,
    payments: list[GenPayment],
    refunds: list[GenRefund],
    settlement: GenSettlement,
    bank_txns: list[GenBankTransaction],
    *,
    settlement_group_payments: list[GenPayment] | None = None,
    settlement_group_refunds: list[GenRefund] | None = None,
    tolerance_paisa: int = DEFAULT_TOLERANCE_PAISA,
) -> DivergenceTrace:
    """Trace one order's chain through to its (already-matched) settlement
    and bank transaction(s).

    `payments`/`refunds` are THIS order's own records (used for the ORDER,
    PAYMENT, and REFUND stages). `settlement_group_payments`/
    `settlement_group_refunds` are the FULL set feeding the settlement —
    a settlement can batch many orders (section 8.4) — and default to
    `payments`/`refunds` for the common single-order-settlement case.
    `bank_txns` is whatever the matcher accepted for this settlement
    (possibly empty — the "genuinely unresolved" case; possibly 2+ —
    duplicate_bank_credit).
    """
    settlement_group_payments = payments if settlement_group_payments is None else settlement_group_payments
    settlement_group_refunds = refunds if settlement_group_refunds is None else settlement_group_refunds

    stages: list[StageResult] = [
        _order_stage(order),
        _payment_stage(order, payments, tolerance_paisa),
        _refund_stage(payments, refunds, tolerance_paisa),
        _settlement_stage(settlement, settlement_group_payments, settlement_group_refunds, tolerance_paisa),
        _bank_stage(settlement, bank_txns, tolerance_paisa),
    ]
    return _build_trace(stages)


def _order_stage(order: GenOrder) -> StageResult:
    # The anchor — nothing upstream to compare against, always consistent
    # with itself.
    return StageResult(
        stage=DivergenceStage.ORDER.value,
        expected_paisa=order.amount_paisa, actual_paisa=order.amount_paisa, delta_paisa=0,
        consistent=True, evidence=[order.order_id], note="anchor of the chain",
    )


def _payment_stage(order: GenOrder, payments: list[GenPayment], tolerance_paisa: int) -> StageResult:
    # Section 12: "Payment expected = Order.amount". Sums all payments so
    # partial_payment (order split across multiple payment records) is
    # handled without special-casing.
    expected = order.amount_paisa
    actual = sum(p.amount_paisa for p in payments)
    delta = actual - expected
    return StageResult(
        stage=DivergenceStage.PAYMENT.value,
        expected_paisa=expected, actual_paisa=actual, delta_paisa=delta,
        consistent=abs(delta) <= tolerance_paisa,
        evidence=[order.order_id] + [p.payment_id for p in payments],
        note=f"{len(payments)} payment(s) totalling {actual}p vs order amount {expected}p",
    )


def _refund_stage(payments: list[GenPayment], refunds: list[GenRefund], tolerance_paisa: int) -> StageResult:
    # Not an equality target — section 12 doesn't give a refund formula, so
    # this is a deliberate, documented gap-fill (see
    # docs/ARCHITECTURE_NOTES.md): a BOUNDS check. "expected" here means
    # "the most that could legitimately be refunded" (what was actually
    # paid), not a target value — a normal partial/full refund should
    # register as fully consistent (delta 0), only an over-refund is a
    # real divergence.
    cap = sum(p.amount_paisa for p in payments)
    actual = sum(r.amount_paisa for r in refunds)
    overage = max(0, actual - cap)
    return StageResult(
        stage=DivergenceStage.REFUND.value,
        expected_paisa=cap, actual_paisa=actual, delta_paisa=overage,
        consistent=overage <= tolerance_paisa,
        evidence=[p.payment_id for p in payments] + [r.refund_id for r in refunds],
        note=(
            f"{len(refunds)} refund(s) totalling {actual}p, within {cap}p paid"
            if overage == 0
            else f"refunds ({actual}p) exceed the {cap}p paid by {overage}p"
        ),
    )


def _settlement_stage(
    settlement: GenSettlement, group_payments: list[GenPayment], group_refunds: list[GenRefund], tolerance_paisa: int
) -> StageResult:
    # Section 12's literal formula, over the FULL settlement group.
    expected = (
        sum(p.amount_paisa for p in group_payments)
        - sum(r.amount_paisa for r in group_refunds)
        - sum(p.fee_paisa for p in group_payments)
        - sum(p.tax_on_fee_paisa for p in group_payments)
    )
    actual = settlement.settled_amount_paisa
    delta = actual - expected
    return StageResult(
        stage=DivergenceStage.SETTLEMENT.value,
        expected_paisa=expected, actual_paisa=actual, delta_paisa=delta,
        consistent=abs(delta) <= tolerance_paisa,
        evidence=(
            [settlement.settlement_id]
            + [p.payment_id for p in group_payments]
            + [r.refund_id for r in group_refunds]
        ),
        note=f"settlement declares {actual}p vs {expected}p expected from {len(group_payments)} payment(s)",
    )


def _bank_stage(settlement: GenSettlement, bank_txns: list[GenBankTransaction], tolerance_paisa: int) -> StageResult:
    # Section 12: "Bank expected = Settlement.settled_amount" — the
    # settlement's own declared value, not a corrected one.
    expected = settlement.settled_amount_paisa
    if not bank_txns:
        return StageResult(
            stage=DivergenceStage.BANK.value,
            expected_paisa=expected, actual_paisa=None, delta_paisa=None,
            consistent=False, evidence=[settlement.settlement_id],
            note="no bank transaction found — cannot verify beyond this point",
        )
    actual = sum(b.amount_paisa for b in bank_txns)
    delta = actual - expected
    return StageResult(
        stage=DivergenceStage.BANK.value,
        expected_paisa=expected, actual_paisa=actual, delta_paisa=delta,
        consistent=abs(delta) <= tolerance_paisa,
        evidence=[settlement.settlement_id] + [b.bank_txn_id for b in bank_txns],
        note=f"{len(bank_txns)} bank txn(s) totalling {actual}p vs settlement's declared {expected}p",
    )


def _build_trace(stages: list[StageResult]) -> DivergenceTrace:
    first_divergence = next((s for s in stages if not s.consistent), None)
    if first_divergence is None:
        return DivergenceTrace(stages=stages, first_divergence=None, downstream_impact=[], status="clean", total_downstream_delta_paisa=0)

    idx = stages.index(first_divergence)
    downstream = stages[idx + 1 :]

    if first_divergence.actual_paisa is None:
        # Missing evidence right at the point of divergence: we know WHERE
        # it broke down, not by how much — reporting a numeric total here
        # would imply a precision we don't have.
        return DivergenceTrace(
            stages=stages, first_divergence=first_divergence, downstream_impact=downstream,
            status="unresolved", total_downstream_delta_paisa=None,
        )

    last_concrete = next((s for s in reversed(stages) if s.actual_paisa is not None), first_divergence)
    total_downstream_delta = last_concrete.actual_paisa - first_divergence.expected_paisa

    return DivergenceTrace(
        stages=stages, first_divergence=first_divergence, downstream_impact=downstream,
        status="diverged", total_downstream_delta_paisa=total_downstream_delta,
    )
