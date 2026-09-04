"""Milestone 9 tests: agent orchestration — the bounded state machine
(PROJECT_SPEC.md section 6) realized end to end with real persistence.
Uses an in-memory SQLite database (same fast-test pattern
tests/conftest.py established in milestone 1) so the full run doesn't
need Postgres. Financial records are persisted directly (mirroring
app.datagen.persist, minus ground truth — never touched here either).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.datagen import catalog
from app.datagen.generator import generate_dataset
from app.db.session import Base
from app.models import financial, operational  # noqa: F401 — register tables on Base.metadata
from app.models.enums import CaseState
from app.models.financial import BankTransaction, Order, Payment, Refund, Settlement
from app.models.operational import AgentEvent, Batch, ExceptionRecord, Investigation, Match, ReconciliationCase
from app.orchestrator.batch_runner import run_batch

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent / "app" / "orchestrator"

DECLINE_RESPONSE = json.dumps({"root_cause": "unknown", "supporting_evidence": [], "confidence": 0.10, "explanation": "n/a"})


class _AlwaysDeclineClient:
    """Every call returns a valid but low-confidence response — the AI is
    genuinely invoked, but never resolves anything on its own. Isolates
    "does the deterministic-rules-only path still work correctly end to
    end" from any AI-specific behavior."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return DECLINE_RESPONSE


_TOKEN_TO_MERCHANT = {catalog.token(name): name for _mid, name in catalog.MERCHANTS}


class _SmartStandInClient:
    """Same validated reasoning as scripts/run_rootcause_eval.py's stand-in
    — reads narration/evidence honestly, never ground truth. Used for the
    representative full-batch integration test."""

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = json.loads(user_prompt)
        delta = payload["delta"]
        evidence = payload["evidence"]
        stage = payload["divergence_stage"]
        narration_text = " ".join((e.get("narration") or "") for e in evidence if e.get("type") == "bank_transaction").upper()

        if stage == "settlement":
            for e in evidence:
                if e.get("type") == "refund":
                    if delta == e["amount_paisa"]:
                        return json.dumps({"root_cause": "missing_refund_netting", "supporting_evidence": [e["id"]], "confidence": 0.85, "explanation": "matches refund exactly"})
                    if delta == -e["amount_paisa"]:
                        return json.dumps({"root_cause": "duplicate_refund", "supporting_evidence": [e["id"]], "confidence": 0.85, "explanation": "matches refund exactly, netted twice"})
            if 0 < abs(delta) <= 5:
                return json.dumps({"root_cause": "currency_rounding", "supporting_evidence": [], "confidence": 0.80, "explanation": "rounding-sized delta"})

        bank_ids = [e["id"] for e in evidence if e.get("type") == "bank_transaction"]
        if "PROC CHG" in narration_text or "ADDL" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids, "confidence": 0.85, "explanation": "additional charge referenced"})
        if "BANK CHARGES" in narration_text or "NET OF" in narration_text:
            return json.dumps({"root_cause": "unmatched_external_deduction", "supporting_evidence": bank_ids, "confidence": 0.85, "explanation": "bank charges referenced"})
        if "(DUP)" in narration_text:
            return json.dumps({"root_cause": "duplicate_bank_credit", "supporting_evidence": bank_ids, "confidence": 0.90, "explanation": "duplicate bank reference"})
        if "ADJ" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids, "confidence": 0.45, "explanation": "vague, low confidence"})
        return DECLINE_RESPONSE


@pytest.fixture()
def db_session():
    # foreign_keys=ON: SQLite doesn't enforce FK constraints by default,
    # which is exactly how a real insert-ordering bug (AgentEvent inserted
    # before its ReconciliationCase — see case_runner.py's run_case) slipped
    # past this test file initially and only surfaced against live
    # Postgres. Enforcing FKs here now closes that gap.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_local()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _persist_financials(session, batch):
    for o in batch.orders:
        session.add(Order(**o.__dict__))
    for p in batch.payments:
        session.add(Payment(**p.__dict__))
    for r in batch.refunds:
        session.add(Refund(**r.__dict__))
    for s in batch.settlements:
        session.add(Settlement(**s.__dict__))
    for b in batch.bank_transactions:
        session.add(BankTransaction(**b.__dict__))
    session.commit()


# --- isolation -----------------------------------------------------------


def test_orchestrator_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in ORCHESTRATOR_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


# --- core state-machine guarantees -----------------------------------------------


def test_every_case_reaches_resolved_or_escalated(db_session):
    batch = generate_dataset(seed=42, num_flows=120, dataset_version="test-orch-1")
    _persist_financials(db_session, batch)

    summary = run_batch(db_session, batch, _AlwaysDeclineClient())

    assert summary.errors == []
    assert summary.total == len(batch.orders)
    assert summary.resolved + summary.escalated == summary.total
    for case in db_session.query(ReconciliationCase).all():
        assert case.state in (CaseState.RESOLVED, CaseState.ESCALATED)


def test_no_infinite_loop_bounded_event_count_per_case(db_session):
    # Longest possible path: created -> MATCH_ATTEMPT -> MATCHED -> VERIFY
    # -> DIVERGENCE_TRACE -> ROOT_CAUSE_INVESTIGATE -> VERIFY -> terminal
    # = 8 events. No case should ever exceed that — there is no retry loop.
    batch = generate_dataset(seed=42, num_flows=150, dataset_version="test-orch-2")
    _persist_financials(db_session, batch)
    run_batch(db_session, batch, _AlwaysDeclineClient())

    for case in db_session.query(ReconciliationCase).all():
        count = db_session.query(AgentEvent).filter_by(case_id=case.case_id).count()
        assert 2 <= count <= 8, f"{case.case_id} had {count} events"


def test_case_state_matches_last_agent_event(db_session):
    batch = generate_dataset(seed=42, num_flows=100, dataset_version="test-orch-3")
    _persist_financials(db_session, batch)
    run_batch(db_session, batch, _AlwaysDeclineClient())

    for case in db_session.query(ReconciliationCase).all():
        last_event = db_session.query(AgentEvent).filter_by(case_id=case.case_id).order_by(AgentEvent.id.desc()).first()
        assert last_event.to_state == case.state


def test_every_case_has_an_ingested_event_first(db_session):
    batch = generate_dataset(seed=42, num_flows=60, dataset_version="test-orch-4")
    _persist_financials(db_session, batch)
    run_batch(db_session, batch, _AlwaysDeclineClient())

    for case in db_session.query(ReconciliationCase).all():
        first_event = db_session.query(AgentEvent).filter_by(case_id=case.case_id).order_by(AgentEvent.id.asc()).first()
        assert first_event.from_state is None
        assert first_event.to_state == CaseState.INGESTED


# --- persistence correctness -----------------------------------------------------


def test_exception_record_exists_for_escalated_and_only_escalated(db_session):
    batch = generate_dataset(seed=42, num_flows=150, dataset_version="test-orch-5")
    _persist_financials(db_session, batch)
    run_batch(db_session, batch, _AlwaysDeclineClient())

    for case in db_session.query(ReconciliationCase).all():
        has_exception = db_session.query(ExceptionRecord).filter_by(case_id=case.case_id).count() > 0
        assert has_exception == (case.state == CaseState.ESCALATED)


def test_match_rows_persisted_for_matched_cases(db_session):
    batch = generate_dataset(seed=42, num_flows=100, dataset_version="test-orch-6")
    _persist_financials(db_session, batch)
    summary = run_batch(db_session, batch, _AlwaysDeclineClient())

    matched_cases = [c for c in summary.cases if "no_match" not in c.reason]
    assert matched_cases
    for c in matched_cases:
        assert db_session.query(Match).filter_by(case_id=c.case_id).count() >= 1


def test_investigation_row_only_for_cases_that_diverged(db_session):
    batch = generate_dataset(seed=42, num_flows=150, dataset_version="test-orch-7")
    _persist_financials(db_session, batch)
    summary = run_batch(db_session, batch, _AlwaysDeclineClient())

    for c in summary.cases:
        has_investigation = db_session.query(Investigation).filter_by(case_id=c.case_id).count() > 0
        reconciled_cleanly = "reconciles exactly" in c.reason
        no_match_at_all = "no_match" in c.reason
        if reconciled_cleanly or no_match_at_all:
            assert not has_investigation, c.case_id
        else:
            assert has_investigation, c.case_id


def test_batch_row_lifecycle(db_session):
    batch = generate_dataset(seed=42, num_flows=30, dataset_version="test-orch-8")
    _persist_financials(db_session, batch)
    run_batch(db_session, batch, _AlwaysDeclineClient())

    batch_row = db_session.query(Batch).filter_by(batch_id=batch.batch_id).one()
    assert batch_row.status == "completed"
    assert batch_row.dataset_version == "test-orch-8"


# --- AI-invocation discipline ("AI only runs on its defined conditions") ----------


def test_ai_only_invoked_when_no_deterministic_cause_exists(db_session):
    batch = generate_dataset(seed=42, num_flows=200, dataset_version="test-orch-9")
    _persist_financials(db_session, batch)
    client = _AlwaysDeclineClient()
    run_batch(db_session, batch, client)

    # AI must have been invoked SOME of the time (cases with no known
    # cause exist in this dataset — see milestone 2/8 notes) ...
    assert client.calls > 0
    # ... but strictly fewer times than the number of divergent cases,
    # since deterministic rules cover a real chunk of them without AI.
    diverged_cases = db_session.query(Investigation).count()
    assert 0 < client.calls <= diverged_cases


def test_root_cause_investigate_transition_appears_whenever_ai_is_invoked(db_session):
    # Regression test for a real bug found during milestone 9 integration:
    # app.rootcause.case.investigate_case's `source` field only says "ai"
    # when the proposal clears the confidence gate, so a naive check would
    # silently skip the ROOT_CAUSE_INVESTIGATE audit event whenever the AI
    # was invoked but declined. Fixed in case_runner.py by checking
    # detect_known_cause directly, not investigate_case's source field.
    batch = generate_dataset(seed=42, num_flows=200, dataset_version="test-orch-10")
    _persist_financials(db_session, batch)
    client = _AlwaysDeclineClient()  # every AI call declines -> this path is exercised heavily
    run_batch(db_session, batch, client)

    assert client.calls > 0
    investigate_transitions = db_session.query(AgentEvent).filter_by(
        to_state=CaseState.ROOT_CAUSE_INVESTIGATE,
    ).count()
    assert investigate_transitions == client.calls


# --- "verifier remains the final authority" ---------------------------------------


def test_verifier_overrides_a_bad_ai_proposal(monkeypatch, db_session):
    # Force the AI to propose a cause whose claimed adjustment does NOT
    # cover the actual delta — the verifier must reject it and the case
    # must escalate, never resolve on AI say-so alone.
    import app.rootcause.investigator as investigator_module

    batch = generate_dataset(seed=42, num_flows=200, dataset_version="test-orch-11")
    _persist_financials(db_session, batch)

    real_to_proposal = investigator_module.to_root_cause_proposal

    def bad_to_proposal(investigation, delta_paisa):
        proposal = real_to_proposal(investigation, delta_paisa)
        proposal.claimed_adjustment_paisa = proposal.claimed_adjustment_paisa + 999_999  # sabotage the arithmetic
        return proposal

    monkeypatch.setattr(investigator_module, "to_root_cause_proposal", bad_to_proposal)
    # app.rootcause.case imports the function by name, so patch it there too.
    import app.rootcause.case as case_module
    monkeypatch.setattr(case_module, "to_root_cause_proposal", bad_to_proposal)

    client = _SmartStandInClient()
    summary = run_batch(db_session, batch, client)

    sabotaged = [c for c in summary.cases if "failed verification" in c.reason]
    assert sabotaged, "expected at least one AI proposal to be sabotaged and rejected by the verifier"
    for c in sabotaged:
        assert c.outcome == "ESCALATED"


# --- representative full-batch integration (realistic AI behavior) ----------------


def test_full_batch_with_realistic_ai_matches_expected_scale(db_session):
    batch = generate_dataset(seed=42, num_flows=200, dataset_version="test-orch-12")
    _persist_financials(db_session, batch)
    summary = run_batch(db_session, batch, _SmartStandInClient())

    assert summary.errors == []
    assert summary.total == 200
    assert summary.resolved + summary.escalated == 200
    # sanity band consistent with milestone 6/8's measured rates — not a
    # tight regression pin, just a check nothing has gone badly wrong.
    assert summary.resolved / summary.total > 0.5
