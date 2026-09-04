"""Milestone 4 tests: the constraint_verifier tool. No database — pure
logic, plus a couple of integration-style tests that feed real
app.datagen/app.matcher output through the verifier (no divergence engine
needed: those tests supply expected/actual directly from generator/ground-
truth-shaped values, the way a future divergence engine eventually will).
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.datagen.generator import generate_dataset
from app.matcher.reconciler import compute_net_contributions, run_deterministic_matching
from app.matcher.types import MatchCandidate
from app.models.enums import MatchMethod, RecordType
from app.verifier.checks import (
    MIN_ROOT_CAUSE_CONFIDENCE,
    verify_chronology,
    verify_no_double_counting,
    verify_reconciliation,
    verify_relationship,
    verify_root_cause_proposal,
)
from app.verifier.types import RootCauseProposal
from app.verifier.verifier import verify_match

VERIFIER_DIR = Path(__file__).resolve().parent.parent / "app" / "verifier"


def _match(source_type=RecordType.PAYMENT.value, source_id="pay_1", target_type=RecordType.SETTLEMENT.value,
           target_id="stl_1", method=MatchMethod.SUBSET_SUM_BATCH.value, score=0.9, accepted=True) -> MatchCandidate:
    return MatchCandidate(source_type=source_type, source_id=source_id, target_type=target_type,
                           target_id=target_id, method=method, score=score, accepted=accepted)


# --- isolation ---------------------------------------------------------------


def test_verifier_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in VERIFIER_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


# --- valid matches -----------------------------------------------------------


def test_valid_match_passes():
    result = verify_match(_match(), expected_paisa=10_500_00, actual_paisa=10_500_00)
    assert result.passed
    assert all(c.passed for c in result.checks)


def test_valid_match_with_chronology_passes():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [("order", t0), ("payment", t0 + timedelta(minutes=1)), ("settlement", t0 + timedelta(days=2))]
    result = verify_match(_match(), expected_paisa=5000, actual_paisa=5000, chronology_events=events)
    assert result.passed


# --- amount / tolerance failures ---------------------------------------------


def test_amount_mismatch_beyond_tolerance_fails():
    result = verify_match(_match(), expected_paisa=10_500_00, actual_paisa=10_350_00)
    assert not result.passed
    failed = {c.name for c in result.failed_checks()}
    assert "amount_arithmetic" in failed


def test_amount_mismatch_within_explicit_tolerance_passes():
    result = verify_match(_match(), expected_paisa=1000, actual_paisa=1003, tolerance_paisa=5)
    assert result.passed


def test_reconciliation_check_exact_zero_tolerance_by_default():
    # section 11: the verifier's own default tolerance is strict (0) - a
    # 1-paisa residual still fails unless a caller explicitly widens it.
    check = verify_reconciliation(1000, 1001)
    assert not check.passed
    check_exact = verify_reconciliation(1000, 1000)
    assert check_exact.passed


def test_relationship_inconsistency_fails_even_with_matching_amounts():
    bad_match = _match(source_type=RecordType.ORDER.value, target_type=RecordType.BANK_TRANSACTION.value)
    result = verify_match(bad_match, expected_paisa=5000, actual_paisa=5000)
    assert not result.passed
    assert any(c.name == "relationship_consistency" and not c.passed for c in result.checks)


def test_verify_relationship_accepts_all_four_chain_hops():
    assert verify_relationship(RecordType.ORDER.value, RecordType.PAYMENT.value).passed
    assert verify_relationship(RecordType.PAYMENT.value, RecordType.REFUND.value).passed
    assert verify_relationship(RecordType.PAYMENT.value, RecordType.SETTLEMENT.value).passed
    assert verify_relationship(RecordType.SETTLEMENT.value, RecordType.BANK_TRANSACTION.value).passed
    assert not verify_relationship(RecordType.REFUND.value, RecordType.SETTLEMENT.value).passed


# --- double-counting -----------------------------------------------------------


def test_double_counting_detected_for_payment_across_two_settlements():
    matches = [
        _match(source_id="pay_1", target_type=RecordType.SETTLEMENT.value, target_id="stl_A"),
        _match(source_id="pay_1", target_type=RecordType.SETTLEMENT.value, target_id="stl_B"),
    ]
    check = verify_no_double_counting(matches)
    assert not check.passed
    assert "pay_1" in check.detail


def test_double_counting_ignores_unaccepted_candidates():
    matches = [
        _match(source_id="pay_1", target_id="stl_A", accepted=True),
        _match(source_id="pay_1", target_id="stl_B", accepted=False),
    ]
    assert verify_no_double_counting(matches).passed


def test_double_counting_allows_one_order_to_many_payments():
    # partial_payment: legitimate one-to-many, must NOT be flagged.
    matches = [
        _match(source_type=RecordType.ORDER.value, source_id="ord_1", target_type=RecordType.PAYMENT.value, target_id="pay_a"),
        _match(source_type=RecordType.ORDER.value, source_id="ord_1", target_type=RecordType.PAYMENT.value, target_id="pay_b"),
    ]
    assert verify_no_double_counting(matches).passed


def test_double_counting_allows_one_settlement_to_many_bank_txns():
    # duplicate_bank_credit: legitimate one-to-many, must NOT be flagged.
    matches = [
        _match(source_type=RecordType.SETTLEMENT.value, source_id="stl_1", target_type=RecordType.BANK_TRANSACTION.value, target_id="bnk_a"),
        _match(source_type=RecordType.SETTLEMENT.value, source_id="stl_1", target_type=RecordType.BANK_TRANSACTION.value, target_id="bnk_b"),
    ]
    assert verify_no_double_counting(matches).passed


def test_double_counting_detected_for_bank_txn_from_two_settlements():
    matches = [
        _match(source_type=RecordType.SETTLEMENT.value, source_id="stl_1", target_type=RecordType.BANK_TRANSACTION.value, target_id="bnk_shared"),
        _match(source_type=RecordType.SETTLEMENT.value, source_id="stl_2", target_type=RecordType.BANK_TRANSACTION.value, target_id="bnk_shared"),
    ]
    check = verify_no_double_counting(matches)
    assert not check.passed
    assert "bnk_shared" in check.detail


# --- invalid dates -------------------------------------------------------------


def test_chronology_valid_sequence_passes():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [("order", t0), ("payment", t0), ("refund", t0 + timedelta(days=1)), ("settlement", t0 + timedelta(days=3))]
    assert verify_chronology(events).passed


def test_chronology_refund_before_payment_fails():
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    events = [("payment", t0), ("refund", t0 - timedelta(days=1))]
    check = verify_chronology(events)
    assert not check.passed
    assert "refund" in check.detail and "payment" in check.detail


def test_chronology_settlement_before_payment_fails():
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    events = [("order", t0), ("payment", t0), ("settlement", t0 - timedelta(hours=1))]
    assert not verify_chronology(events).passed


def test_verify_match_fails_on_bad_chronology_even_with_correct_amount():
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bad_events = [("payment", t0), ("refund", t0 - timedelta(days=2))]
    result = verify_match(_match(), expected_paisa=100, actual_paisa=100, chronology_events=bad_events)
    assert not result.passed
    assert any(c.name == "chronology" and not c.passed for c in result.checks)


# --- invalid AI / root-cause proposals ----------------------------------------


def test_root_cause_proposal_spec_worked_example_passes():
    # PROJECT_SPEC.md section 11's own example, verbatim (in paisa):
    # expected=10,500 actual=10,350 delta=150, AI claims fee=150 -> PASS.
    proposal = RootCauseProposal(
        root_cause="unreported_fee", claimed_adjustment_paisa=-15_000,
        confidence=0.89, supporting_evidence_ids=["fee_123"],
    )
    result = verify_root_cause_proposal(
        proposal, expected_paisa=1_050_000, actual_paisa=1_035_000, known_evidence_ids={"fee_123"},
    )
    assert result.passed


def test_root_cause_proposal_unbounded_cause_fails():
    # section 10: "the AI cannot invent new cause categories" - even with
    # perfect confidence/arithmetic/evidence, an unbounded cause fails.
    proposal = RootCauseProposal(
        root_cause="the_dog_ate_my_ledger", claimed_adjustment_paisa=-15_000,
        confidence=0.99, supporting_evidence_ids=["fee_123"],
    )
    result = verify_root_cause_proposal(
        proposal, expected_paisa=1_050_000, actual_paisa=1_035_000, known_evidence_ids={"fee_123"},
    )
    assert not result.passed
    assert any(c.name == "bounded_root_cause" and not c.passed for c in result.checks)


def test_root_cause_proposal_low_confidence_fails_even_with_correct_arithmetic():
    # section 11: "AI confidence is never enough" - here read as its
    # converse too: confidence below the section 10 gate fails regardless
    # of otherwise-perfect arithmetic.
    proposal = RootCauseProposal(
        root_cause="unreported_fee", claimed_adjustment_paisa=-15_000,
        confidence=0.40, supporting_evidence_ids=["fee_123"],
    )
    result = verify_root_cause_proposal(
        proposal, expected_paisa=1_050_000, actual_paisa=1_035_000, known_evidence_ids={"fee_123"},
    )
    assert not result.passed
    assert any(c.name == "confidence_gate" and not c.passed for c in result.checks)
    assert proposal.confidence < MIN_ROOT_CAUSE_CONFIDENCE


def test_root_cause_proposal_arithmetic_mismatch_fails():
    proposal = RootCauseProposal(
        root_cause="unreported_fee", claimed_adjustment_paisa=-10_000,  # claims ₹100, actual gap is ₹150
        confidence=0.89, supporting_evidence_ids=["fee_123"],
    )
    result = verify_root_cause_proposal(
        proposal, expected_paisa=1_050_000, actual_paisa=1_035_000, known_evidence_ids={"fee_123"},
    )
    assert not result.passed
    assert any(c.name == "root_cause_amount_coverage" and not c.passed for c in result.checks)


def test_root_cause_proposal_unknown_evidence_fails():
    proposal = RootCauseProposal(
        root_cause="unreported_fee", claimed_adjustment_paisa=-15_000,
        confidence=0.89, supporting_evidence_ids=["fee_does_not_exist"],
    )
    result = verify_root_cause_proposal(
        proposal, expected_paisa=1_050_000, actual_paisa=1_035_000, known_evidence_ids={"fee_123"},
    )
    assert not result.passed
    assert any(c.name == "evidence_referenced" and not c.passed for c in result.checks)


def test_root_cause_proposal_no_evidence_fails():
    proposal = RootCauseProposal(
        root_cause="unreported_fee", claimed_adjustment_paisa=-15_000,
        confidence=0.89, supporting_evidence_ids=[],
    )
    result = verify_root_cause_proposal(
        proposal, expected_paisa=1_050_000, actual_paisa=1_035_000, known_evidence_ids={"fee_123"},
    )
    assert not result.passed
    assert any(c.name == "evidence_referenced" and not c.passed for c in result.checks)


# --- AI-derived match consistency: no special leniency for AI-assisted -------


def test_ai_assisted_match_gets_no_leniency_on_bad_amounts():
    ai_match = _match(method=MatchMethod.NARRATION_AI_ASSISTED.value)
    deterministic_match = _match(method=MatchMethod.SUBSET_SUM_BATCH.value)

    ai_result = verify_match(ai_match, expected_paisa=1000, actual_paisa=1500)
    det_result = verify_match(deterministic_match, expected_paisa=1000, actual_paisa=1500)

    assert not ai_result.passed
    assert not det_result.passed
    assert [c.passed for c in ai_result.checks] == [c.passed for c in det_result.checks]


# --- integration: real generated data through the matcher, then verifier ----


def test_clean_settlement_from_real_data_passes_verification():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-verifier")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    contributions = {c.payment_id: c for c in compute_net_contributions(batch.payments, batch.refunds, batch.orders)}
    settlements_by_id = {s.settlement_id: s for s in batch.settlements}
    gt_by_order = {g.record_id: g for g in batch.ground_truth}
    payment_to_order = {p.payment_id: p.order_id for p in batch.payments}

    accepted = [m for m in result.settlement_payment if m.accepted]
    checked = 0
    for m in accepted:
        gt = gt_by_order[payment_to_order[m.source_id]]
        if gt.true_root_cause is not None:
            continue  # only genuinely clean settlements here
        members = [x for x in accepted if x.target_id == m.target_id]
        actual = sum(contributions[x.source_id].net_contribution_paisa for x in members)
        expected = settlements_by_id[m.target_id].settled_amount_paisa
        v = verify_match(m, expected_paisa=expected, actual_paisa=actual)
        assert v.passed, f"{m.target_id}: {v.failed_checks()}"
        checked += 1
    assert checked > 10


def test_unreported_fee_settlement_fails_plain_verify_but_passes_with_root_cause():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-verifier-2")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    contributions = {c.payment_id: c for c in compute_net_contributions(batch.payments, batch.refunds, batch.orders)}
    settlements_by_id = {s.settlement_id: s for s in batch.settlements}
    gt_by_order = {g.record_id: g for g in batch.ground_truth}
    payment_to_order = {p.payment_id: p.order_id for p in batch.payments}
    accepted = [m for m in result.settlement_payment if m.accepted]

    found = False
    for m in accepted:
        gt = gt_by_order[payment_to_order[m.source_id]]
        if gt.true_root_cause != "unreported_fee":
            continue
        members = [x for x in accepted if x.target_id == m.target_id]
        # "expected" is what the settlement SHOULD be (baseline recomputed
        # from the underlying payment/refund records); "actual" is what the
        # settlement DECLARES. For unreported_fee the two differ by an
        # undocumented fee.
        expected = sum(contributions[x.source_id].net_contribution_paisa for x in members)
        actual_declared = settlements_by_id[m.target_id].settled_amount_paisa

        plain = verify_match(m, expected_paisa=expected, actual_paisa=actual_declared)
        assert not plain.passed  # no root cause applied yet -> must fail, not silently pass

        delta = actual_declared - expected
        proposal = RootCauseProposal(
            root_cause="unreported_fee", claimed_adjustment_paisa=delta,
            confidence=0.9, supporting_evidence_ids=["ev_1"],
        )
        with_cause = verify_root_cause_proposal(proposal, expected, actual_declared, known_evidence_ids={"ev_1"})
        assert with_cause.passed
        found = True
        break

    assert found, "expected at least one unreported_fee settlement in this seed's data"
