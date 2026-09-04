"""Milestone 1 smoke tests: model metadata is well-formed and ground truth is
structurally isolated from the main schema.

These run against an in-memory SQLite database as a fast import/DDL sanity
check. They do not substitute for running `alembic upgrade head` against the
real Postgres instance (see README) — Postgres-specific behaviour (schemas,
native types) is not exercised here.
"""
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from app.db.session import Base
from app.models.enums import CaseState, RecordType
from app.models.financial import Order, Payment, Refund, Settlement
from app.models.operational import Batch, ReconciliationCase


def test_main_metadata_contains_all_core_tables():
    table_names = set(Base.metadata.tables.keys())
    expected = {
        "orders",
        "payments",
        "refunds",
        "settlements",
        "bank_transactions",
        "batches",
        "reconciliation_cases",
        "matches",
        "agent_events",
        "evidence",
        "investigations",
        "exceptions",
        "evaluation_runs",
    }
    assert expected.issubset(table_names)


def test_ground_truth_is_not_in_main_metadata():
    # Ground truth lives on a completely separate Base/metadata (see
    # app/db/groundtruth_session.py) so nothing importing app.db.session.Base
    # ever sees it.
    assert "ground_truth" not in Base.metadata.tables


def test_settlement_has_no_payment_foreign_key():
    # Section 4: the payment-to-settlement mapping must not be exposed via a
    # convenient foreign key. Discovering it is the matcher's job.
    settlement_columns = {c.name for c in Settlement.__table__.columns}
    assert "payment_id" not in settlement_columns


def test_order_to_payment_to_refund_chain_round_trip(db_session):
    order = Order(
        order_id="order_001",
        merchant_id="merchant_001",
        amount_paisa=100000,
        currency="INR",
        status="paid",
    )
    db_session.add(order)
    db_session.flush()

    payment = Payment(
        payment_id="pay_001",
        order_id="order_001",
        amount_paisa=100000,
        fee_paisa=2000,
        tax_on_fee_paisa=360,
        method="upi",
        status="captured",
        narration="UPI/order_001/merchant_001",
    )
    db_session.add(payment)
    db_session.flush()

    refund = Refund(
        refund_id="rfnd_001",
        payment_id="pay_001",
        amount_paisa=10000,
        reason_code="customer_request",
        narration="partial refund",
    )
    db_session.add(refund)
    db_session.commit()

    stored_payment = db_session.query(Payment).filter_by(payment_id="pay_001").one()
    assert stored_payment.order_id == "order_001"
    assert stored_payment.amount_paisa == 100000  # integer paisa, no float drift
    assert isinstance(stored_payment.amount_paisa, int)

    stored_refund = db_session.query(Refund).filter_by(refund_id="rfnd_001").one()
    assert stored_refund.payment_id == "pay_001"


def test_reconciliation_case_defaults_to_ingested_state(db_session):
    batch = Batch(batch_id="batch_001", dataset_version="v1")
    db_session.add(batch)
    db_session.flush()

    case = ReconciliationCase(
        case_id="case_001",
        batch_id="batch_001",
        anchor_type=RecordType.SETTLEMENT,
        anchor_id="settle_001",
    )
    db_session.add(case)
    db_session.commit()

    stored = db_session.query(ReconciliationCase).filter_by(case_id="case_001").one()
    assert stored.state == CaseState.INGESTED


def test_case_state_rejects_values_outside_the_bounded_enum(db_session):
    batch = Batch(batch_id="batch_002", dataset_version="v1")
    db_session.add(batch)
    db_session.flush()

    case = ReconciliationCase(
        case_id="case_002",
        batch_id="batch_002",
        anchor_type=RecordType.SETTLEMENT,
        anchor_id="settle_002",
        state="NOT_A_REAL_STATE",
    )
    db_session.add(case)
    try:
        db_session.commit()
        assert False, "expected an invalid enum value to be rejected"
    except StatementError as exc:
        assert isinstance(exc.orig, LookupError)
        db_session.rollback()


def test_enum_columns_persist_the_dot_value_not_the_member_name(db_session):
    # Regression guard: SQLAlchemy's Enum type stores the Python enum
    # member's `.name` by default (e.g. "SETTLEMENT"), not its `.value`
    # ("settlement") — a real bug found while verifying this milestone
    # against live Postgres (see docs/ARCHITECTURE_NOTES.md). CaseState
    # masked it because its member names equal their values; RecordType
    # does not. app.models.enums.sa_enum() fixes this with
    # values_callable=... — this test locks that in.
    batch = Batch(batch_id="batch_003", dataset_version="v1")
    db_session.add(batch)
    db_session.flush()

    case = ReconciliationCase(
        case_id="case_003",
        batch_id="batch_003",
        anchor_type=RecordType.SETTLEMENT,
        anchor_id="settle_003",
    )
    db_session.add(case)
    db_session.commit()

    raw_value = db_session.execute(
        text("SELECT anchor_type FROM reconciliation_cases WHERE case_id = 'case_003'")
    ).scalar_one()
    assert raw_value == "settlement"  # RecordType.SETTLEMENT.value, not "SETTLEMENT"
