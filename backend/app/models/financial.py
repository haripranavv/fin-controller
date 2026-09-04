"""Core financial entities: Order, Payment, Refund, Settlement, Bank Transaction.

PROJECT_SPEC.md section 4. Money is always integer paisa (BigInteger) — never
Float/Numeric for a monetary amount anywhere in this codebase.

Deliberately NOT present: any foreign key from Settlement to Payment. The
payment-to-settlement relationship must be discovered by the deterministic
matcher (bounded subset-sum over candidates) — it is not given for free by
the schema. See test_settlement_has_no_payment_foreign_key.
"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, String, func

from app.db.session import Base
from app.db.types import BIGINT_PK


class Order(Base):
    __tablename__ = "orders"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    merchant_id = Column(String(64), nullable=False, index=True)
    amount_paisa = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    payment_id = Column(String(64), unique=True, nullable=False, index=True)
    order_id = Column(String(64), ForeignKey("orders.order_id"), nullable=False, index=True)
    amount_paisa = Column(BigInteger, nullable=False)
    fee_paisa = Column(BigInteger, nullable=False, default=0, server_default="0")
    tax_on_fee_paisa = Column(BigInteger, nullable=False, default=0, server_default="0")
    method = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    narration = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Refund(Base):
    __tablename__ = "refunds"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    refund_id = Column(String(64), unique=True, nullable=False, index=True)
    payment_id = Column(String(64), ForeignKey("payments.payment_id"), nullable=False, index=True)
    amount_paisa = Column(BigInteger, nullable=False)
    reason_code = Column(String(64), nullable=True)
    narration = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Settlement(Base):
    __tablename__ = "settlements"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    settlement_id = Column(String(64), unique=True, nullable=False, index=True)
    merchant_id = Column(String(64), nullable=False, index=True)
    settled_amount_paisa = Column(BigInteger, nullable=False)
    fee_deducted_paisa = Column(BigInteger, nullable=False, default=0, server_default="0")
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    bank_txn_id = Column(String(64), unique=True, nullable=False, index=True)
    amount_paisa = Column(BigInteger, nullable=False)
    value_date = Column(DateTime(timezone=True), nullable=False)
    utr_ref = Column(String(64), nullable=True, index=True)
    narration = Column(String(512), nullable=True)
