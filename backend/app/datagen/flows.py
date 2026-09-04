"""Axis-A flow builders: for one order, decide how many payments/refunds it
has and how hard its narration is to match. This is independent of whether
the flow's eventual settlement carries a genuine financial divergence —
that's axis B, applied later in settlement.py once flows are grouped (see
app/datagen/models.py's module docstring for the axis A/B split).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.datagen import catalog
from app.datagen.models import (
    AXIS_A_CLEAN,
    AXIS_A_DELAYED_EVENT,
    AXIS_A_DUPLICATE_REFERENCE,
    AXIS_A_MESSY_NARRATION,
    AXIS_A_PARTIAL_PAYMENT,
    AXIS_A_REFUND_FULL,
    AXIS_A_REFUND_PARTIAL,
    GenOrder,
    GenPayment,
    GenRefund,
    OrderFlow,
    order_id as make_order_id,
    payment_id as make_payment_id,
    refund_id as make_refund_id,
)

# Relative sampling weights (random.choices normalizes; they don't need to
# sum to 1). Messy narration weighted high since it's the dimension section
# 16 explicitly wants measured (AI lift on unseen formats vs baseline).
AXIS_A_WEIGHTS: dict[str, int] = {
    AXIS_A_CLEAN: 30,
    AXIS_A_MESSY_NARRATION: 16,
    AXIS_A_DUPLICATE_REFERENCE: 7,
    AXIS_A_DELAYED_EVENT: 9,
    AXIS_A_PARTIAL_PAYMENT: 9,
    AXIS_A_REFUND_PARTIAL: 9,
    AXIS_A_REFUND_FULL: 6,
}


def random_amount_paisa(rng: random.Random, lo_rupees: int = 50, hi_rupees: int = 25_000) -> int:
    return rng.randint(lo_rupees, hi_rupees) * 100


def fee_and_tax(amount_paisa: int, rng: random.Random) -> tuple[int, int]:
    fee_rate = rng.uniform(0.015, 0.025)
    fee = round(amount_paisa * fee_rate)
    tax = round(fee * 0.18)  # GST on the fee -> Payment.tax_on_fee_paisa
    return fee, tax


def _clean_narration(rng: random.Random, merchant_name: str, order_id_: str, payment_id_: str, customer: str) -> str:
    template = rng.choice(catalog.CLEAN_NARRATION_TEMPLATES)
    return template.format(
        payment_id=payment_id_,
        merchant_short=catalog.short_code(merchant_name),
        merchant_upper=merchant_name.upper(),
        order_id=order_id_,
        customer_token=catalog.token(customer)[:10],
    )


def _messy_narration(
    rng: random.Random,
    counterparty_name: str,
    invoice_num: str,
    ref_token: str,
    date_token: str,
    amount_hint: int,
) -> str:
    template = rng.choice(catalog.MESSY_NARRATION_TEMPLATES)
    return template.format(
        bank_code=rng.choice(catalog.BANK_CODES),
        counterparty_token=catalog.token(counterparty_name),
        counterparty_name=counterparty_name,
        invoice_num=invoice_num,
        ref_token=ref_token,
        date_token=date_token,
        amount_hint=amount_hint,
    )


def build_order_flow(
    rng: random.Random,
    dataset_version: str,
    idx: int,
    merchant_id: str,
    merchant_name: str,
    created_at: datetime,
    category: str,
    shared_ref_token: str | None = None,
) -> OrderFlow:
    """Build one OrderFlow for `category`. `shared_ref_token` is only used
    for AXIS_A_DUPLICATE_REFERENCE, where the caller (generator.py) pairs
    two flows onto the same invoice/reference token so the matcher has to
    disambiguate them by amount+date rather than reference alone."""
    oid = make_order_id(dataset_version, idx)
    customer = rng.choice(catalog.CUSTOMER_NAMES)

    if category == AXIS_A_PARTIAL_PAYMENT:
        total_amount = random_amount_paisa(rng)
        split = rng.uniform(0.35, 0.65)
        part_a = round(total_amount * split)
        parts = [part_a, total_amount - part_a]
        payments = []
        for i, part_amount in enumerate(parts):
            fee, tax = fee_and_tax(part_amount, rng)
            pid = make_payment_id(dataset_version, idx, suffix=f"_{chr(97 + i)}")
            narration = _clean_narration(rng, merchant_name, oid, pid, customer) + " PARTIAL"
            payments.append(
                GenPayment(
                    payment_id=pid,
                    order_id=oid,
                    amount_paisa=part_amount,
                    fee_paisa=fee,
                    tax_on_fee_paisa=tax,
                    method=rng.choice(catalog.PAYMENT_METHODS),
                    status="captured",
                    narration=narration,
                    created_at=created_at + timedelta(minutes=rng.randint(30, 240) * (i + 1)),
                )
            )
        refunds: list[GenRefund] = []

    else:
        total_amount = random_amount_paisa(rng)
        fee, tax = fee_and_tax(total_amount, rng)
        pid = make_payment_id(dataset_version, idx)
        payment_created_at = created_at
        if category == AXIS_A_DELAYED_EVENT:
            payment_created_at = created_at + timedelta(days=rng.randint(5, 12), hours=rng.randint(0, 23))

        if category in (AXIS_A_MESSY_NARRATION, AXIS_A_DUPLICATE_REFERENCE):
            invoice_num = shared_ref_token or str(rng.randint(10000, 99999))
            ref_token = shared_ref_token or f"REF{rng.randint(1000, 9999)}"
            narration = _messy_narration(
                rng,
                counterparty_name=merchant_name,
                invoice_num=invoice_num,
                ref_token=ref_token,
                date_token=payment_created_at.strftime("%d%m"),
                amount_hint=total_amount // 100,
            )
        else:
            narration = _clean_narration(rng, merchant_name, oid, pid, customer)

        payments = [
            GenPayment(
                payment_id=pid,
                order_id=oid,
                amount_paisa=total_amount,
                fee_paisa=fee,
                tax_on_fee_paisa=tax,
                method=rng.choice(catalog.PAYMENT_METHODS),
                status="captured",
                narration=narration,
                created_at=payment_created_at,
            )
        ]

        refunds = []
        if category in (AXIS_A_REFUND_PARTIAL, AXIS_A_REFUND_FULL):
            refund_amount = (
                total_amount if category == AXIS_A_REFUND_FULL else round(total_amount * rng.uniform(0.3, 0.7))
            )
            refunds.append(
                GenRefund(
                    refund_id=make_refund_id(dataset_version, idx),
                    payment_id=pid,
                    amount_paisa=refund_amount,
                    reason_code=rng.choice(catalog.REFUND_REASON_CODES),
                    narration=f"REFUND {pid}",
                    created_at=payment_created_at + timedelta(days=rng.randint(1, 5)),
                )
            )

    order = GenOrder(
        order_id=oid,
        merchant_id=merchant_id,
        amount_paisa=sum(p.amount_paisa for p in payments),
        currency="INR",
        status="paid",
        created_at=created_at,
    )
    return OrderFlow(
        order=order,
        payments=payments,
        refunds=refunds,
        axis_a_category=category,
        merchant_name=merchant_name,
        flow_idx=idx,
    )
