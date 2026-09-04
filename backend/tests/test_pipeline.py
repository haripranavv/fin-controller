"""Milestone 6 tests: the deterministic end-to-end pipeline. Hand-crafted
fixtures for precise control over every state-machine branch, plus a
full-batch integration run proving the section 21 Definition of DONE
requirement ("every case reaches RESOLVED or ESCALATED") against real
generated data.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.datagen.generator import generate_dataset
from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.divergence.types import StageResult
from app.matcher.reconciler import run_deterministic_matching
from app.pipeline.assemble import assemble_case_inputs
from app.pipeline.known_causes import CURRENCY_ROUNDING_MAX_PAISA, detect_known_cause
from app.pipeline.pipeline import resolve_case
from app.pipeline.types import CaseInputs

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "app" / "pipeline"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_order(order_id="ord_1", amount=10_000) -> GenOrder:
    return GenOrder(order_id=order_id, merchant_id="mch_1", amount_paisa=amount, currency="INR", status="paid", created_at=BASE)


def make_payment(payment_id="pay_1", order_id="ord_1", amount=10_000, fee=0, tax=0) -> GenPayment:
    return GenPayment(payment_id=payment_id, order_id=order_id, amount_paisa=amount, fee_paisa=fee,
                       tax_on_fee_paisa=tax, method="upi", status="captured", narration=None, created_at=BASE)


def make_refund(refund_id="rfd_1", payment_id="pay_1", amount=1_000) -> GenRefund:
    return GenRefund(refund_id=refund_id, payment_id=payment_id, amount_paisa=amount,
                      reason_code="customer_request", narration=None, created_at=BASE)


def make_settlement(settlement_id="stl_1", settled=10_000) -> GenSettlement:
    return GenSettlement(settlement_id=settlement_id, merchant_id="mch_1", settled_amount_paisa=settled,
                          fee_deducted_paisa=0, period_start=BASE, period_end=BASE, created_at=BASE)


def make_bank_txn(bank_txn_id="bnk_1", amount=10_000) -> GenBankTransaction:
    return GenBankTransaction(bank_txn_id=bank_txn_id, amount_paisa=amount, value_date=BASE, utr_ref="UTR1", narration=None)


def make_settlement_match(payment_id="pay_1", settlement_id="stl_1"):
    from app.matcher.types import MatchCandidate
    from app.models.enums import MatchMethod, RecordType
    return MatchCandidate(source_type=RecordType.PAYMENT.value, source_id=payment_id,
                           target_type=RecordType.SETTLEMENT.value, target_id=settlement_id,
                           method=MatchMethod.SUBSET_SUM_BATCH.value, score=0.99, accepted=True)


# --- isolation -----------------------------------------------------------


def test_pipeline_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in PIPELINE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


# --- known_causes.py unit tests ------------------------------------------------


def test_known_cause_missing_refund_netting():
    refund = make_refund(refund_id="rfd_A", amount=1_500)
    stage = StageResult(stage="settlement", expected_paisa=8_500, actual_paisa=10_000, delta_paisa=1_500,
                         consistent=False, evidence=["stl_1"], note="")
    proposal = detect_known_cause(stage, [refund], [])
    assert proposal is not None
    assert proposal.root_cause == "missing_refund_netting"
    assert proposal.claimed_adjustment_paisa == 1_500
    assert proposal.supporting_evidence_ids == ["rfd_A"]
    assert proposal.confidence == 1.0


def test_known_cause_duplicate_refund():
    refund = make_refund(refund_id="rfd_B", amount=1_500)
    stage = StageResult(stage="settlement", expected_paisa=10_000, actual_paisa=8_500, delta_paisa=-1_500,
                         consistent=False, evidence=["stl_1"], note="")
    proposal = detect_known_cause(stage, [refund], [])
    assert proposal.root_cause == "duplicate_refund"
    assert proposal.claimed_adjustment_paisa == -1_500


def test_known_cause_currency_rounding_within_band():
    stage = StageResult(stage="settlement", expected_paisa=10_000, actual_paisa=9_997, delta_paisa=-3,
                         consistent=False, evidence=["stl_1"], note="")
    proposal = detect_known_cause(stage, [], [])
    assert proposal.root_cause == "currency_rounding"
    assert proposal.claimed_adjustment_paisa == -3
    assert proposal.supporting_evidence_ids == ["stl_1"]


def test_known_cause_currency_rounding_does_not_fire_above_band():
    stage = StageResult(stage="settlement", expected_paisa=10_000,
                         actual_paisa=10_000 - (CURRENCY_ROUNDING_MAX_PAISA + 1),
                         delta_paisa=-(CURRENCY_ROUNDING_MAX_PAISA + 1),
                         consistent=False, evidence=["stl_1"], note="")
    assert detect_known_cause(stage, [], []) is None


def test_known_cause_duplicate_bank_credit():
    bank_a = make_bank_txn("bnk_A", 5_000)
    bank_b = make_bank_txn("bnk_B", 5_000)
    stage = StageResult(stage="bank", expected_paisa=5_000, actual_paisa=10_000, delta_paisa=5_000,
                         consistent=False, evidence=["stl_1"], note="")
    proposal = detect_known_cause(stage, [], [bank_a, bank_b])
    assert proposal.root_cause == "duplicate_bank_credit"
    assert set(proposal.supporting_evidence_ids) == {"bnk_A", "bnk_B"}


def test_known_cause_duplicate_bank_credit_requires_exactly_two_and_exact_double():
    bank_a = make_bank_txn("bnk_A", 5_000)
    stage_one_txn = StageResult(stage="bank", expected_paisa=5_000, actual_paisa=10_000, delta_paisa=5_000,
                                 consistent=False, evidence=["stl_1"], note="")
    assert detect_known_cause(stage_one_txn, [], [bank_a]) is None  # only one bank txn

    bank_b = make_bank_txn("bnk_B", 4_000)  # not an exact double
    stage_uneven = StageResult(stage="bank", expected_paisa=5_000, actual_paisa=9_000, delta_paisa=4_000,
                                consistent=False, evidence=["stl_1"], note="")
    assert detect_known_cause(stage_uneven, [], [bank_a, bank_b]) is None


def test_known_cause_returns_none_for_unreported_fee_shaped_delta():
    # A mid-sized delta with no matching refund and outside the rounding
    # band — exactly what unreported_fee/ambiguous_cause look like. Must
    # NOT match any rule (they genuinely need AI).
    stage = StageResult(stage="settlement", expected_paisa=10_000, actual_paisa=9_850, delta_paisa=-150,
                         consistent=False, evidence=["stl_1"], note="")
    assert detect_known_cause(stage, [], []) is None


def test_known_cause_returns_none_when_evidence_missing():
    stage = StageResult(stage="bank", expected_paisa=5_000, actual_paisa=None, delta_paisa=None,
                         consistent=False, evidence=["stl_1"], note="")
    assert detect_known_cause(stage, [], []) is None


# --- resolve_case: hand-crafted, one branch per state-machine path -----------


def test_resolve_case_clean_chain_resolves():
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[make_payment(amount=10_000, fee=200, tax=36)], refunds=[],
        settlement=make_settlement(settled=10_000 - 236), settlement_match=make_settlement_match(),
        bank_txns=[make_bank_txn(amount=10_000 - 236)],
    )
    result = resolve_case(inputs)
    assert result.outcome == "RESOLVED"
    assert "reconciles exactly" in result.reason
    assert result.trace.status == "clean"
    assert result.verification.passed


def test_resolve_case_no_match_escalates():
    inputs = CaseInputs(order=make_order(), payments=[make_payment()], refunds=[], settlement=None, settlement_match=None)
    result = resolve_case(inputs)
    assert result.outcome == "ESCALATED"
    assert "no_match" in result.reason
    assert result.trace is None


def test_resolve_case_known_cause_missing_refund_netting_resolves():
    # Correctly-netted expected = payment(10,000) - refund(1,500) = 8,500.
    # Settlement declares 10,000 - i.e. it forgot to subtract the refund at
    # all - so delta = 10,000 - 8,500 = +1,500, exactly the refund amount.
    payment = make_payment(amount=10_000)
    refund = make_refund(amount=1_500)
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[payment], refunds=[],
        settlement=make_settlement(settled=10_000),  # forgot to net the refund
        settlement_match=make_settlement_match(),
        settlement_group_payments=[payment], settlement_group_refunds=[refund],
        bank_txns=[make_bank_txn(amount=10_000)],
    )
    result = resolve_case(inputs)
    assert result.outcome == "RESOLVED"
    assert result.root_cause_proposal.root_cause == "missing_refund_netting"
    assert "known cause" in result.reason


def test_resolve_case_known_cause_duplicate_bank_credit_resolves():
    payment = make_payment(amount=10_000)
    settlement = make_settlement(settled=10_000)
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[payment], refunds=[],
        settlement=settlement, settlement_match=make_settlement_match(),
        bank_txns=[make_bank_txn("bnk_A", 10_000), make_bank_txn("bnk_B", 10_000)],
    )
    result = resolve_case(inputs)
    assert result.outcome == "RESOLVED"
    assert result.root_cause_proposal.root_cause == "duplicate_bank_credit"


def test_resolve_case_unknown_cause_escalates():
    payment = make_payment(amount=10_000)
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[payment], refunds=[],
        settlement=make_settlement(settled=9_850),  # a -150 gap matching no rule (unreported_fee-shaped)
        settlement_match=make_settlement_match(),
        bank_txns=[make_bank_txn(amount=9_850)],
    )
    result = resolve_case(inputs)
    assert result.outcome == "ESCALATED"
    assert "no known deterministic cause" in result.reason
    assert result.trace.first_divergence.stage == "settlement"


def test_resolve_case_unresolved_missing_bank_escalates():
    payment = make_payment(amount=10_000)
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[payment], refunds=[],
        settlement=make_settlement(settled=10_000), settlement_match=make_settlement_match(),
        bank_txns=[],
    )
    result = resolve_case(inputs)
    assert result.outcome == "ESCALATED"
    assert "unresolved" in result.reason
    assert result.trace.status == "unresolved"


def test_resolve_case_known_cause_proposal_that_fails_verification_escalates(monkeypatch):
    # Every CURRENT rule in known_causes.py sets claimed_adjustment_paisa =
    # delta exactly, which makes root_cause_amount_coverage a tautology —
    # today's rules can never actually fail verification once they fire.
    # resolve_case's "proposal found but failed verification -> ESCALATED"
    # branch exists as defensive programming for future non-tautological
    # rules (and eventually AI proposals, which have no such guarantee).
    # Exercise it directly by forcing a deliberately-wrong proposal.
    import app.pipeline.pipeline as pipeline_module
    from app.verifier.types import RootCauseProposal

    payment = make_payment(amount=10_000)
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[payment], refunds=[],
        settlement=make_settlement(settled=9_850), settlement_match=make_settlement_match(),
        bank_txns=[make_bank_txn(amount=9_850)],
    )
    bad_proposal = RootCauseProposal(
        root_cause="unreported_fee", claimed_adjustment_paisa=-50,  # actual gap is -150
        confidence=1.0, supporting_evidence_ids=[],
    )
    monkeypatch.setattr(pipeline_module, "detect_known_cause", lambda *a, **kw: bad_proposal)

    result = resolve_case(inputs)
    assert result.outcome == "ESCALATED"
    assert "failed verification" in result.reason


def test_resolve_case_currency_rounding_resolves_within_zero_tolerance():
    payment = make_payment(amount=10_000)
    inputs = CaseInputs(
        order=make_order(amount=10_000), payments=[payment], refunds=[],
        settlement=make_settlement(settled=10_000 - 3), settlement_match=make_settlement_match(),
        bank_txns=[make_bank_txn(amount=10_000 - 3)],
    )
    result = resolve_case(inputs, tolerance_paisa=0)
    assert result.outcome == "RESOLVED"
    assert result.root_cause_proposal.root_cause == "currency_rounding"


# --- full-batch integration: section 21 DoD ("every case RESOLVED or ESCALATED") --


@pytest.fixture(scope="module")
def batch_and_result():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-pipeline")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    return batch, result


def test_every_case_reaches_resolved_or_escalated(batch_and_result):
    batch, result = batch_and_result
    for order in batch.orders:
        inputs = assemble_case_inputs(batch, result, order.order_id)
        case_result = resolve_case(inputs)
        assert case_result.outcome in ("RESOLVED", "ESCALATED"), f"{order.order_id}: got {case_result.outcome!r}"


def test_clean_cases_resolve_and_known_causes_resolve(batch_and_result):
    batch, result = batch_and_result
    gt_by_order = {g.record_id: g for g in batch.ground_truth}
    resolvable_causes = {None, "missing_refund_netting", "duplicate_refund", "currency_rounding", "duplicate_bank_credit"}

    checked, resolved = 0, 0
    for order in batch.orders:
        gt = gt_by_order[order.order_id]
        if gt.true_root_cause not in resolvable_causes:
            continue
        inputs = assemble_case_inputs(batch, result, order.order_id)
        if inputs.settlement_match is None:
            continue  # a matcher miss, not this test's concern (see milestone 3 notes)
        case_result = resolve_case(inputs)
        checked += 1
        if case_result.outcome == "RESOLVED":
            resolved += 1
    assert checked > 20
    assert resolved / checked >= 0.85, f"only {resolved}/{checked} deterministically-resolvable cases actually resolved"


def test_ai_needed_cases_escalate(batch_and_result):
    batch, result = batch_and_result
    gt_by_order = {g.record_id: g for g in batch.ground_truth}
    ai_needed_causes = {"unreported_fee", "unmatched_external_deduction", "ambiguous_cause", "unknown"}

    checked = 0
    for order in batch.orders:
        gt = gt_by_order[order.order_id]
        if gt.true_root_cause not in ai_needed_causes:
            continue
        inputs = assemble_case_inputs(batch, result, order.order_id)
        if inputs.settlement_match is None:
            continue
        case_result = resolve_case(inputs)
        assert case_result.outcome == "ESCALATED", (
            f"{order.order_id} (true cause {gt.true_root_cause!r}) resolved without AI — "
            f"should be impossible: {case_result.reason}"
        )
        checked += 1
    assert checked > 5
