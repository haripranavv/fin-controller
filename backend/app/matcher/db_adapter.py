"""Loads a dataset_version's financial records from Postgres into the same
plain dataclasses app.datagen.models uses, so app.matcher's core logic never
needs to know whether its input came from a freshly generated in-memory
batch or a persisted one. Read-only — this package does not write matches
(see app/matcher/__init__.py).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.models.financial import BankTransaction, Order, Payment, Refund, Settlement


def load_dataset(
    session: Session, dataset_version: str
) -> tuple[list[GenOrder], list[GenPayment], list[GenRefund], list[GenSettlement], list[GenBankTransaction]]:
    prefix = f"%_{dataset_version}_%"

    orders = [
        GenOrder(
            order_id=o.order_id, merchant_id=o.merchant_id, amount_paisa=o.amount_paisa,
            currency=o.currency, status=o.status, created_at=o.created_at,
        )
        for o in session.query(Order).filter(Order.order_id.like(prefix)).all()
    ]
    payments = [
        GenPayment(
            payment_id=p.payment_id, order_id=p.order_id, amount_paisa=p.amount_paisa,
            fee_paisa=p.fee_paisa, tax_on_fee_paisa=p.tax_on_fee_paisa, method=p.method,
            status=p.status, narration=p.narration, created_at=p.created_at,
        )
        for p in session.query(Payment).filter(Payment.payment_id.like(prefix)).all()
    ]
    refunds = [
        GenRefund(
            refund_id=r.refund_id, payment_id=r.payment_id, amount_paisa=r.amount_paisa,
            reason_code=r.reason_code, narration=r.narration, created_at=r.created_at,
        )
        for r in session.query(Refund).filter(Refund.refund_id.like(prefix)).all()
    ]
    settlements = [
        GenSettlement(
            settlement_id=s.settlement_id, merchant_id=s.merchant_id,
            settled_amount_paisa=s.settled_amount_paisa, fee_deducted_paisa=s.fee_deducted_paisa,
            period_start=s.period_start, period_end=s.period_end, created_at=s.created_at,
        )
        for s in session.query(Settlement).filter(Settlement.settlement_id.like(prefix)).all()
    ]
    bank_txns = [
        GenBankTransaction(
            bank_txn_id=b.bank_txn_id, amount_paisa=b.amount_paisa,
            value_date=b.value_date, utr_ref=b.utr_ref, narration=b.narration,
        )
        for b in session.query(BankTransaction).filter(BankTransaction.bank_txn_id.like(prefix)).all()
    ]

    return orders, payments, refunds, settlements, bank_txns
