"""File import flow: deterministic source-type detection (app.api.
import_detect, pure functions - unit tested directly), the async job
HTTP endpoints (app.api.routes_import, against the SQLite db_session
fixture every other API test uses) - create/list/get/confirm, the
QUEUED->VALIDATING->IMPORTING->READY/FAILED lifecycle, duplicate-
submission prevention, a genuinely failed import, the bulk-insert path
at a larger row count, cross-user isolation, and one true end-to-end
test proving imported rows reach the real, UNCHANGED
app.orchestrator.batch_runner.run_batch and come out RESOLVED - the same
function every synthetic dataset in this project already goes through,
never a second reconciliation path.
"""
from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.import_detect import GROUND_TRUTH_MARKER_COLUMNS, detect_source_type, parse_csv, validate_rows
from app.api.routes_auth import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.auth import User
from app.models.enums import CaseState
from app.models.financial import Order, Payment
from app.models.import_job import ImportJob
from app.models.operational import Batch, ReconciliationCase
from app.rootcause.client import MockRootCauseClient

ORDERS_CSV = b"order_id,merchant_id,amount_paisa,currency,status\nTESTORD1,merchant_x,100000,INR,paid\n"
PAYMENTS_CSV = b"payment_id,order_id,amount_paisa,fee_paisa,tax_on_fee_paisa,method,status\nTESTPAY1,TESTORD1,100000,0,0,card,captured\n"

# Order/Payment.created_at defaults to func.now() at insert time (no
# created_at column in the upload format), so the settlement window and
# bank value_date below are built relative to "now" - not a fixed past
# date - to fall inside app.matcher.reconciler's real date-proximity
# windows, the same way app.datagen.generator's own synthetic flows do.
_now = datetime.now(timezone.utc)
_period_start = (_now - timedelta(hours=6)).isoformat()
_period_end = (_now + timedelta(hours=6)).isoformat()
_bank_value_date = (_now + timedelta(days=2)).isoformat()

SETTLEMENTS_CSV = (
    "settlement_id,merchant_id,settled_amount_paisa,fee_deducted_paisa,period_start,period_end\n"
    f"TESTSTL1,merchant_x,100000,0,{_period_start},{_period_end}\n"
).encode()
BANK_CSV = f"bank_txn_id,amount_paisa,value_date,utr_ref\nTESTBNK1,100000,{_bank_value_date},UTRJOB1\n".encode()


# --- pure detection/validation (no DB, no HTTP) --------------------------------------


def test_detect_source_type_for_each_real_type():
    for csv_bytes, expected in [
        (ORDERS_CSV, "order"), (PAYMENTS_CSV, "payment"),
        (SETTLEMENTS_CSV, "settlement"), (BANK_CSV, "bank_transaction"),
    ]:
        columns, _rows = parse_csv(csv_bytes)
        detected, missing = detect_source_type(columns)
        assert detected == expected, f"{csv_bytes!r} -> {detected}, missing={missing}"


def test_detect_rejects_ground_truth_shaped_columns():
    gt_csv = b"record_id,true_root_cause,true_match_ids,is_ambiguous\nord_x_00001,unreported_fee,[],false\n"
    columns, _ = parse_csv(gt_csv)
    detected, _ = detect_source_type(columns)
    assert detected == "rejected_ground_truth"
    assert set(columns) & GROUND_TRUTH_MARKER_COLUMNS


def test_detect_unknown_for_unrecognized_columns_reports_closest_missing():
    columns, _ = parse_csv(b"order_id,merchant_id\nX,Y\n")  # missing amount_paisa, status
    detected, missing = detect_source_type(columns)
    assert detected == "unknown"
    assert "amount_paisa" in missing and "status" in missing


def test_validate_rows_flags_invalid_amount_and_missing_fields():
    columns, rows = parse_csv(b"order_id,merchant_id,amount_paisa,status\nA,merchant_x,not-a-number,paid\nB,,100,paid\n")
    result = validate_rows("order", rows)
    assert len(result.valid_rows) == 0
    assert result.invalid_row_count == 2
    assert result.sample_errors


def test_validate_rows_flags_duplicate_primary_key():
    columns, rows = parse_csv(b"order_id,merchant_id,amount_paisa,status\nA,m,100,paid\nA,m,200,paid\n")
    result = validate_rows("order", rows)
    assert len(result.valid_rows) == 1
    assert result.duplicate_count == 1


def test_validate_rows_accepts_a_clean_file():
    columns, rows = parse_csv(ORDERS_CSV)
    result = validate_rows("order", rows)
    assert len(result.valid_rows) == 1
    assert result.invalid_row_count == 0
    assert result.duplicate_count == 0


# --- HTTP endpoints (SQLite fixture, real auth via get_current_user override) -------


@pytest.fixture()
def make_client(db_session):
    """Returns a factory so a test can create clients for two DIFFERENT,
    REAL, concurrently-live users sharing the same SQLite session.

    app.dependency_overrides is a single dict on the shared `app` object,
    not per-TestClient - overriding get_current_user per user (like
    test_api.py's single-user fixture does) would make the LAST client
    created win for every request from EVERY client, silently attributing
    all of them to the same identity. So here get_current_user is left as
    the real dependency, and each client instead registers a real user via
    /api/auth/register and carries a real bearer token as a default
    header - only get_db is overridden (to the shared in-memory session)."""
    def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    bootstrap = TestClient(app)

    def _make(user_id: int, email: str) -> TestClient:
        token = bootstrap.post("/api/auth/register", json={"email": email, "password": "correcthorse"}).json()["token"]
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    yield _make
    app.dependency_overrides.clear()


@pytest.fixture()
def client(db_session):
    user = User(id=1, email="importer@example.com", password_hash="unused", is_demo=False)

    def _get_db_override():
        yield db_session

    def _get_current_user_override():
        return user

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_current_user] = _get_current_user_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _upload(client, files: dict[str, bytes]):
    return client.post("/api/import/jobs", files=[("files", (name, io.BytesIO(content), "text/csv")) for name, content in files.items()])


def _wait_ready_or_failed(client, job_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/api/import/jobs/{job_id}").json()
        if body["status"] in ("READY", "FAILED"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach READY/FAILED within {timeout_s}s")


def test_create_job_reports_ready_files_and_status_validating(client):
    resp = _upload(client, {"orders.csv": ORDERS_CSV, "payments.csv": PAYMENTS_CSV})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VALIDATING"
    assert body["any_ready"] is True
    by_type = {f["detected_type"]: f for f in body["files"]}
    assert by_type["order"]["ready"] is True
    assert by_type["order"]["valid_row_count"] == 1


def test_create_job_rejects_ground_truth_file(client):
    resp = _upload(client, {"gt.csv": b"record_id,true_root_cause\nord_x_1,unreported_fee\n"})
    body = resp.json()
    assert body["files"][0]["detected_type"] == "rejected_ground_truth"
    assert body["any_ready"] is False


def test_create_job_invalid_file_handling_bad_csv_bytes(client):
    resp = client.post("/api/import/jobs", files=[("files", ("bad.csv", io.BytesIO(b"\xff\xfe not really csv \x00\x01"), "text/csv"))])
    assert resp.status_code == 200  # a bad upload is a validation result, never a 500


# --- async job lifecycle --------------------------------------------------------------


def test_job_lifecycle_reaches_ready_and_creates_batch(client, db_session):
    job_id = _upload(client, {"orders.csv": ORDERS_CSV, "payments.csv": PAYMENTS_CSV,
                               "settlements.csv": SETTLEMENTS_CSV, "bank.csv": BANK_CSV}).json()["job_id"]
    confirm = client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "lifecycle1"})
    assert confirm.status_code == 200
    # The background thread may finish before this response is even
    # serialized on a tiny fixture like this one - both are legitimate
    # points in the real QUEUED->VALIDATING->IMPORTING->READY lifecycle.
    assert confirm.json()["status"] in ("IMPORTING", "READY")

    final = _wait_ready_or_failed(client, job_id)
    assert final["status"] == "READY"
    assert final["batch_id"] == "batch_lifecycle1"
    assert final["rows_inserted"] == 4

    assert db_session.query(Batch).filter_by(batch_id="batch_lifecycle1").count() == 1
    orders = db_session.query(Order).filter(Order.order_id.like("%_lifecycle1_%")).all()
    assert len(orders) == 1
    payments = db_session.query(Payment).filter(Payment.order_id == orders[0].order_id).all()
    assert len(payments) == 1  # FK correctly rewritten to the generated order_id, not the raw "TESTORD1"


def test_job_survives_being_polled_after_the_upload_request_ends(client):
    """Simulates "leaving the Import page and coming back": the job is
    looked up fresh by job_id in a later, independent request, not held
    in any request-scoped state."""
    job_id = _upload(client, {"orders.csv": ORDERS_CSV}).json()["job_id"]
    # A brand new "page load" - just a GET by id, no upload involved.
    resp = client.get(f"/api/import/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "VALIDATING"
    assert resp.json()["files"][0]["valid_row_count"] == 1


def test_list_jobs_shows_history_for_the_current_user(client):
    _upload(client, {"orders.csv": ORDERS_CSV})
    _upload(client, {"orders.csv": ORDERS_CSV})
    resp = client.get("/api/import/jobs")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_unknown_job_404s(client):
    resp = client.get("/api/import/jobs/does-not-exist")
    assert resp.status_code == 404


# --- duplicate submission / failure handling ------------------------------------------


def test_duplicate_confirm_is_rejected(client):
    job_id = _upload(client, {"orders.csv": ORDERS_CSV}).json()["job_id"]
    first = client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "dupguard1"})
    assert first.status_code == 200
    second = client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "dupguard1"})
    assert second.status_code == 409


def test_confirm_rejects_dataset_version_collision(client):
    job_id1 = _upload(client, {"orders.csv": ORDERS_CSV}).json()["job_id"]
    client.post(f"/api/import/jobs/{job_id1}/confirm", json={"dataset_version": "collide1"})
    job_id2 = _upload(client, {"orders.csv": ORDERS_CSV}).json()["job_id"]
    resp = client.post(f"/api/import/jobs/{job_id2}/confirm", json={"dataset_version": "collide1"})
    assert resp.status_code == 409


def test_confirm_with_no_valid_rows_is_rejected(client):
    bad_csv = b"order_id,merchant_id,amount_paisa,status\n,,not-a-number,\n"
    job_id = _upload(client, {"orders.csv": bad_csv}).json()["job_id"]
    resp = client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "nothingvalid1"})
    assert resp.status_code == 400


def test_import_job_failed_state_when_all_rows_unresolvable(client, db_session):
    """A payment file whose every row references an order_id that was
    never uploaded - nothing resolves, nothing inserts, the job
    transitions to FAILED with a real error_message rather than
    silently reporting READY with zero rows."""
    orphan_payment = b"payment_id,order_id,amount_paisa,fee_paisa,tax_on_fee_paisa,method,status\nPAYX,NO_SUCH_ORDER,100,0,0,card,captured\n"
    job_id = _upload(client, {"payments.csv": orphan_payment}).json()["job_id"]
    client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "orphantest1"})
    final = _wait_ready_or_failed(client, job_id)
    assert final["status"] == "FAILED"
    assert final["error_message"]
    assert db_session.query(Batch).filter_by(batch_id="batch_orphantest1").count() == 0


# --- bulk insert path (efficiency mechanism, not a special-cased scale) ---------------


def test_bulk_import_path_inserts_many_rows_via_one_statement_per_type(client, db_session):
    n = 500
    orders_csv = "order_id,merchant_id,amount_paisa,currency,status\n" + "".join(
        f"BULKORD{i},merchant_bulk,{1000 + i},INR,paid\n" for i in range(n)
    )
    job_id = _upload(client, {"orders.csv": orders_csv.encode()}).json()["job_id"]
    confirm = client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "bulktest1"})
    assert confirm.status_code == 200
    final = _wait_ready_or_failed(client, job_id, timeout_s=10.0)
    assert final["status"] == "READY"
    assert final["rows_inserted"] == n
    assert db_session.query(Order).filter(Order.order_id.like("%_bulktest1_%")).count() == n


# --- cross-user isolation --------------------------------------------------------------


def test_user_cannot_see_another_users_import_job(make_client):
    alice = make_client(1, "alice@example.com")
    bob = make_client(2, "bob@example.com")
    job_id = _upload(alice, {"orders.csv": ORDERS_CSV}).json()["job_id"]

    resp = bob.get(f"/api/import/jobs/{job_id}")
    assert resp.status_code == 404
    assert bob.get("/api/import/jobs").json() == []


def test_user_cannot_see_another_users_batch_or_cases(make_client, db_session):
    alice = make_client(1, "alice2@example.com")
    bob = make_client(2, "bob2@example.com")
    job_id = _upload(alice, {"orders.csv": ORDERS_CSV, "payments.csv": PAYMENTS_CSV,
                              "settlements.csv": SETTLEMENTS_CSV, "bank.csv": BANK_CSV}).json()["job_id"]
    alice.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "isoltest1"})
    _wait_ready_or_failed(alice, job_id)

    assert alice.get("/api/overview", params={"batch_id": "batch_isoltest1"}).status_code == 200
    assert bob.get("/api/overview", params={"batch_id": "batch_isoltest1"}).status_code == 404
    assert bob.get("/api/cases", params={"batch_id": "batch_isoltest1"}).status_code == 404
    assert "isoltest1" not in [b["dataset_version"] for b in bob.get("/api/batches").json()]
    assert "isoltest1" in [b["dataset_version"] for b in alice.get("/api/batches").json()]


# --- true end-to-end: imported data reaches the real, unchanged controller -----------


def test_imported_batch_reaches_the_real_controller_and_resolves(client, db_session):
    """Not a second reconciliation path - this calls app.orchestrator.
    batch_runner.run_batch directly (the exact function POST /api/runs
    calls in production, unchanged), proving imported rows flow through
    the real matcher/verifier/divergence pipeline like any other batch."""
    job_id = _upload(client, {"orders.csv": ORDERS_CSV, "payments.csv": PAYMENTS_CSV,
                               "settlements.csv": SETTLEMENTS_CSV, "bank.csv": BANK_CSV}).json()["job_id"]
    client.post(f"/api/import/jobs/{job_id}/confirm", json={"dataset_version": "e2eimport1"})
    final = _wait_ready_or_failed(client, job_id)
    assert final["status"] == "READY"

    from app.datagen.models import GeneratedBatch
    from app.matcher.db_adapter import load_dataset
    from app.orchestrator.batch_runner import run_batch

    orders, payments, refunds, settlements, bank_txns = load_dataset(db_session, "e2eimport1")
    assert len(orders) == 1 and len(payments) == 1 and len(settlements) == 1 and len(bank_txns) == 1

    batch = GeneratedBatch(batch_id=final["batch_id"], dataset_version="e2eimport1", seed=0,
                            orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)
    summary = run_batch(db_session, batch, MockRootCauseClient(default="{}"))

    assert summary.total == 1
    case = db_session.query(ReconciliationCase).filter_by(batch_id=final["batch_id"]).first()
    assert case is not None
    assert case.state == CaseState.RESOLVED
