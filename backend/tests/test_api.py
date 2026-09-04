"""Milestone 11 tests: the operator-console API layer.

Isolation test mirrors every other package's (app.datagen, app.matcher,
app.divergence, ...) test_*_does_not_import_groundtruth pattern. The
smoke tests exercise the real FastAPI app end-to-end against an in-memory
SQLite session (app.db.session.get_db overridden, same db_session fixture
every other DB-touching test uses) - not a substitute for the manual
verification against live Postgres in docs/ARCHITECTURE_NOTES.md, but
enough to catch a route/schema mismatch fast.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.routes_auth import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.auth import User
from app.models.enums import CaseState, DivergenceStage, MatchMethod, RecordType, RootCause, Severity
from app.models.financial import Order
from app.models.operational import AgentEvent, Batch, ExceptionRecord, Investigation, Match, ReconciliationCase

API_DIR = Path(__file__).resolve().parent.parent / "app" / "api"


def test_api_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in API_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


@pytest.fixture()
def client(db_session):
    """These tests are about route/schema shape, not auth itself (see
    test_auth.py for that) - get_current_user is overridden to a fixed
    test user so every route under test still runs through its real
    ownership checks (batch_visibility_filter/require_visible_batch)
    against a real, consistent identity, without needing a real
    register/login round trip in every test."""
    test_user = User(id=1, email="test-api@example.com", password_hash="unused", is_demo=False)

    def _get_db_override():
        yield db_session

    def _get_current_user_override():
        return test_user

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_current_user] = _get_current_user_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _seed_one_resolved_case(db_session):
    db_session.add(Batch(batch_id="batch_t1", dataset_version="t1", status="completed"))
    db_session.add(Order(order_id="ord_t1_00001", merchant_id="m1", amount_paisa=10000, currency="INR", status="captured"))
    db_session.flush()
    case = ReconciliationCase(case_id="case_ord_t1_00001", batch_id="batch_t1", anchor_type=RecordType.ORDER,
                               anchor_id="ord_t1_00001", state=CaseState.RESOLVED)
    db_session.add(case)
    db_session.flush()
    db_session.add(AgentEvent(case_id=case.case_id, from_state=None, to_state=CaseState.INGESTED, message="case created"))
    db_session.add(AgentEvent(case_id=case.case_id, from_state=CaseState.VERIFY, to_state=CaseState.RESOLVED,
                               tool="constraint_verifier", message="chain reconciles exactly",
                               verifier_result={"passed": True, "checks": []}))
    db_session.add(Match(case_id=case.case_id, source_type=RecordType.PAYMENT, source_id="pay_t1_00001",
                          target_type=RecordType.SETTLEMENT, target_id="stl_t1_00001", method=MatchMethod.EXACT_REFERENCE,
                          score=1.0, accepted=True))
    db_session.commit()
    return case


def _seed_one_escalated_case(db_session):
    db_session.add(Order(order_id="ord_t1_00002", merchant_id="m1", amount_paisa=5000, currency="INR", status="captured"))
    db_session.flush()
    case = ReconciliationCase(case_id="case_ord_t1_00002", batch_id="batch_t1", anchor_type=RecordType.ORDER,
                               anchor_id="ord_t1_00002", state=CaseState.ESCALATED)
    db_session.add(case)
    db_session.flush()
    db_session.add(AgentEvent(case_id=case.case_id, from_state=CaseState.VERIFY, to_state=CaseState.ESCALATED,
                               tool="escalate", message="escalated: proposed cause 'unreported_fee' (ai) failed verification",
                               verifier_result={"passed": False, "checks": [{"name": "arithmetic", "passed": False, "detail": "delta mismatch"}]}))
    db_session.add(Investigation(case_id=case.case_id, divergence_stage=DivergenceStage.SETTLEMENT,
                                  expected_amount_paisa=5000, actual_amount_paisa=4800, delta_paisa=200,
                                  root_cause=RootCause.UNREPORTED_FEE, confidence=0.62, status="rejected"))
    db_session.add(ExceptionRecord(case_id=case.case_id, reason="escalated: proposed cause failed verification",
                                    severity=Severity.MEDIUM, amount_paisa=5000, status="open"))
    db_session.commit()
    return case


def test_overview(client, db_session):
    _seed_one_resolved_case(db_session)
    _seed_one_escalated_case(db_session)
    resp = client.get("/api/overview", params={"batch_id": "batch_t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cases"] == 2
    assert body["resolved"] == 1
    assert body["escalated"] == 1
    assert body["exception_count"] == 1
    assert body["exception_value_paisa"] == 5000


def test_overview_404_when_no_batches(client):
    resp = client.get("/api/overview")
    assert resp.status_code == 404


def test_batches_list(client, db_session):
    _seed_one_resolved_case(db_session)
    resp = client.get("/api/batches")
    assert resp.status_code == 200
    batches = resp.json()
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "batch_t1"
    assert batches[0]["resolved"] == 1


def test_case_list_and_filter(client, db_session):
    _seed_one_resolved_case(db_session)
    _seed_one_escalated_case(db_session)
    resp = client.get("/api/cases", params={"batch_id": "batch_t1"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    resp = client.get("/api/cases", params={"batch_id": "batch_t1", "state": "ESCALATED"})
    assert resp.json()["total"] == 1
    assert resp.json()["cases"][0]["case_id"] == "case_ord_t1_00002"

    resp = client.get("/api/cases", params={"batch_id": "batch_t1", "q": "00001"})
    assert resp.json()["total"] == 1


def test_case_detail(client, db_session):
    case = _seed_one_resolved_case(db_session)
    resp = client.get(f"/api/cases/{case.case_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "RESOLVED"
    assert body["order"]["record_id"] == "ord_t1_00001"
    assert len(body["events"]) == 2
    assert len(body["matches"]) == 1


def test_case_detail_404(client):
    resp = client.get("/api/cases/does-not-exist")
    assert resp.status_code == 404


def test_case_investigation_no_settlement_still_returns(client, db_session):
    db_session.add(Batch(batch_id="batch_t1", dataset_version="t1", status="completed"))
    db_session.add(Order(order_id="ord_t1_00003", merchant_id="m1", amount_paisa=1000, currency="INR", status="captured"))
    db_session.flush()
    case = ReconciliationCase(case_id="case_ord_t1_00003", batch_id="batch_t1", anchor_type=RecordType.ORDER,
                               anchor_id="ord_t1_00003", state=CaseState.ESCALATED)
    db_session.add(case)
    db_session.commit()
    resp = client.get(f"/api/cases/{case.case_id}/investigation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain_available"] is False
    assert body["chain"] == []


def test_exceptions_list(client, db_session):
    db_session.add(Batch(batch_id="batch_t1", dataset_version="t1", status="completed"))
    db_session.commit()
    _seed_one_escalated_case(db_session)
    resp = client.get("/api/exceptions", params={"batch_id": "batch_t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["exceptions"][0]
    assert item["root_cause"] == "unreported_fee"
    assert item["verifier_result"]["passed"] is False


def test_run_status_unknown_batch_reports_not_running(client):
    resp = client.get("/api/runs/batch_unknown/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False


def test_batch_events_empty_for_unknown_batch(client):
    resp = client.get("/api/runs/batch_unknown/events")
    assert resp.status_code == 200
    assert resp.json() == []
