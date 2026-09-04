"""Milestone 5 tests: the divergence_tracer tool. No database — pure logic
against hand-crafted fixtures (for precise, easy-to-verify numbers) plus
integration tests against real app.datagen/app.matcher output (mirroring
what a future orchestrator would assemble from the matcher's accepted
matches for one case).
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.datagen.generator import generate_dataset
from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.divergence.tracer import trace_chain
from app.matcher.reconciler import run_deterministic_matching

DIVERGENCE_DIR = Path(__file__).resolve().parent.parent / "app" / "divergence"

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_order(order_id="ord_1", amount=10_000, merchant="mch_1", created_at=BASE) -> GenOrder:
    return GenOrder(order_id=order_id, merchant_id=merchant, amount_paisa=amount, currency="INR", status="paid", created_at=created_at)


def make_payment(payment_id="pay_1", order_id="ord_1", amount=10_000, fee=0, tax=0, created_at=BASE) -> GenPayment:
    return GenPayment(payment_id=payment_id, order_id=order_id, amount_paisa=amount, fee_paisa=fee,
                       tax_on_fee_paisa=tax, method="upi", status="captured", narration=None, created_at=created_at)


def make_refund(refund_id="rfd_1", payment_id="pay_1", amount=1_000, created_at=BASE) -> GenRefund:
    return GenRefund(refund_id=refund_id, payment_id=payment_id, amount_paisa=amount,
                      reason_code="customer_request", narration=None, created_at=created_at)


def make_settlement(settlement_id="stl_1", merchant="mch_1", settled=10_000, created_at=BASE) -> GenSettlement:
    return GenSettlement(settlement_id=settlement_id, merchant_id=merchant, settled_amount_paisa=settled,
                          fee_deducted_paisa=0, period_start=created_at, period_end=created_at, created_at=created_at)


def make_bank_txn(bank_txn_id="bnk_1", amount=10_000, value_date=BASE) -> GenBankTransaction:
    return GenBankTransaction(bank_txn_id=bank_txn_id, amount_paisa=amount, value_date=value_date,
                               utr_ref="UTR1", narration=None)


# --- isolation ---------------------------------------------------------------


def test_divergence_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in DIVERGENCE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


# --- clean chains --------------------------------------------------------------


def test_clean_chain_hand_crafted():
    order = make_order(amount=10_000)
    payment = make_payment(amount=10_000, fee=200, tax=36)
    settlement = make_settlement(settled=10_000 - 200 - 36)
    bank = make_bank_txn(amount=10_000 - 200 - 36)

    trace = trace_chain(order, [payment], [], settlement, [bank])

    assert trace.status == "clean"
    assert trace.first_divergence is None
    assert trace.downstream_impact == []
    assert trace.total_downstream_delta_paisa == 0
    assert all(s.consistent for s in trace.stages)
    assert [s.stage for s in trace.stages] == ["order", "payment", "refund", "settlement", "bank"]


def test_clean_settlement_from_real_generated_data():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-clean")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    gt_by_order = {g.record_id: g for g in batch.ground_truth}

    checked = 0
    for gt in batch.ground_truth:
        if gt.true_root_cause is not None:
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        assert trace.status == "clean", f"{order.order_id}: expected clean, got {trace.status} ({trace.first_divergence})"
        checked += 1
    assert checked > 10


# --- fee differences -----------------------------------------------------------


def test_unreported_fee_diverges_at_settlement_then_cascades_clean():
    order = make_order(amount=10_000)
    payment = make_payment(amount=10_000, fee=0, tax=0)
    # correct expected = 10,000; settlement under-declares by 150 (an
    # unreported fee) but the bank leg faithfully reflects what settlement
    # declared.
    settlement = make_settlement(settled=9_850)
    bank = make_bank_txn(amount=9_850)

    trace = trace_chain(order, [payment], [], settlement, [bank])

    assert trace.status == "diverged"
    assert trace.first_divergence.stage == "settlement"
    assert trace.first_divergence.delta_paisa == -150
    assert len(trace.downstream_impact) == 1
    assert trace.downstream_impact[0].stage == "bank"
    assert trace.downstream_impact[0].consistent  # no NEW problem at bank
    assert trace.total_downstream_delta_paisa == -150  # original impact persists to the end


def test_unreported_fee_from_real_generated_data():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-fee")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    found = False
    for gt in batch.ground_truth:
        if gt.true_root_cause != "unreported_fee":
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        assert trace.status == "diverged"
        assert trace.first_divergence.stage == "settlement"
        assert trace.first_divergence.delta_paisa < 0  # settlement under-declares
        found = True
        break
    assert found, "expected at least one accepted-and-matched unreported_fee case in this seed's data"


# --- refunds ---------------------------------------------------------------------


def test_refund_within_bounds_is_consistent():
    order = make_order(amount=10_000)
    payment = make_payment(amount=10_000)
    refund = make_refund(amount=3_000)
    settlement = make_settlement(settled=10_000 - 3_000)
    bank = make_bank_txn(amount=10_000 - 3_000)

    trace = trace_chain(order, [payment], [refund], settlement, [bank])
    refund_stage = next(s for s in trace.stages if s.stage == "refund")
    assert refund_stage.consistent
    assert refund_stage.delta_paisa == 0
    assert trace.status == "clean"


def test_refund_exceeding_payment_diverges_at_refund_stage():
    order = make_order(amount=10_000)
    payment = make_payment(amount=10_000)
    over_refund = make_refund(amount=12_000)  # invalid: refunded more than paid
    settlement = make_settlement(settled=10_000 - 12_000)
    bank = make_bank_txn(amount=10_000 - 12_000)

    trace = trace_chain(order, [payment], [over_refund], settlement, [bank])

    assert trace.first_divergence.stage == "refund"
    assert trace.first_divergence.delta_paisa == 2_000  # overage
    assert not trace.first_divergence.consistent


def test_refund_full_from_real_generated_data_is_consistent():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-refund")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    checked = 0
    for gt in batch.ground_truth:
        if gt.true_root_cause is not None or "refund_full" not in gt.injected_noise_type and "refund_partial" not in gt.injected_noise_type:
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        if not refunds:
            continue
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        refund_stage = next(s for s in trace.stages if s.stage == "refund")
        assert refund_stage.consistent, f"{order.order_id}: {refund_stage}"
        checked += 1
    assert checked > 0


# --- delayed events --------------------------------------------------------------


def test_delayed_event_does_not_cause_false_divergence():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-delayed")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    checked = 0
    for gt in batch.ground_truth:
        if gt.true_root_cause is not None or gt.injected_noise_type.split("+")[0] != "delayed_event":
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue  # matcher didn't confidently match this one — not this test's concern
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        assert trace.status == "clean", "a timing delay alone must not register as an amount divergence"
        checked += 1
    assert checked > 0


# --- batched settlements -----------------------------------------------------------


def test_batched_settlement_uses_full_group_for_settlement_expected():
    order1 = make_order(order_id="ord_1", amount=5_000)
    payment1 = make_payment(payment_id="pay_1", order_id="ord_1", amount=5_000, fee=100, tax=18)
    order2 = make_order(order_id="ord_2", amount=3_000)
    payment2 = make_payment(payment_id="pay_2", order_id="ord_2", amount=3_000, fee=60, tax=11)

    group_total_expected = (5_000 + 3_000) - (100 + 60) - (18 + 11)  # 7,811
    settlement = make_settlement(settled=group_total_expected)
    bank = make_bank_txn(amount=group_total_expected)

    trace = trace_chain(
        order1, [payment1], [], settlement, [bank],
        settlement_group_payments=[payment1, payment2], settlement_group_refunds=[],
    )

    payment_stage = next(s for s in trace.stages if s.stage == "payment")
    assert payment_stage.actual_paisa == 5_000  # only order1's OWN payment, not the group
    settlement_stage = next(s for s in trace.stages if s.stage == "settlement")
    assert settlement_stage.expected_paisa == group_total_expected  # the FULL group
    assert trace.status == "clean"


def test_batched_settlement_from_real_generated_data():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-batch")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    accepted_settlement = [m for m in result.settlement_payment if m.accepted]
    group_sizes: dict[str, int] = {}
    for m in accepted_settlement:
        group_sizes[m.target_id] = group_sizes.get(m.target_id, 0) + 1
    multi_member_settlements = {sid for sid, n in group_sizes.items() if n >= 2}
    assert multi_member_settlements, "expected at least one batched (multi-payment) settlement in this seed's data"

    gt_by_order = {g.record_id: g for g in batch.ground_truth}
    checked = 0
    for gt in batch.ground_truth:
        if gt.true_root_cause is not None:
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        if settlement.settlement_id not in multi_member_settlements:
            continue
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        assert trace.status == "clean"
        settlement_stage = next(s for s in trace.stages if s.stage == "settlement")
        assert len(settlement_stage.evidence) >= 3  # settlement_id + 2+ payment_ids at least
        checked += 1
    assert checked > 0


# --- rounding / tolerance -----------------------------------------------------------


def test_currency_rounding_flagged_at_zero_tolerance_but_clean_with_widened_tolerance():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-rounding")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    found = False
    for gt in batch.ground_truth:
        if gt.true_root_cause != "currency_rounding":
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built

        strict = trace_chain(order, payments, refunds, settlement, bank_txns,
                              settlement_group_payments=group_payments, settlement_group_refunds=group_refunds,
                              tolerance_paisa=0)
        assert strict.status == "diverged"
        assert strict.first_divergence.stage == "settlement"
        assert 0 < abs(strict.first_divergence.delta_paisa) <= 5  # generator's currency_rounding band

        widened = trace_chain(order, payments, refunds, settlement, bank_txns,
                               settlement_group_payments=group_payments, settlement_group_refunds=group_refunds,
                               tolerance_paisa=5)
        assert widened.status == "clean"
        found = True
        break
    assert found, "expected at least one accepted-and-matched currency_rounding case in this seed's data"


# --- multiple downstream discrepancies ------------------------------------------------


def test_multiple_independent_downstream_discrepancies():
    order = make_order(amount=10_000)
    payment = make_payment(amount=10_000)
    settlement = make_settlement(settled=9_850)  # -150 vs expected 10,000
    bank = make_bank_txn(amount=9_750)  # a SECOND, independent -100 problem at bank

    trace = trace_chain(order, [payment], [], settlement, [bank])

    assert trace.first_divergence.stage == "settlement"
    assert trace.first_divergence.delta_paisa == -150
    assert len(trace.downstream_impact) == 1
    bank_stage = trace.downstream_impact[0]
    assert bank_stage.stage == "bank"
    assert not bank_stage.consistent
    assert bank_stage.delta_paisa == -100  # a NEW divergence, not a repeat of -150
    assert trace.total_downstream_delta_paisa == -250  # cumulative: -150 + -100


def test_payment_stage_divergence_cascades_cleanly_when_downstream_is_internally_consistent():
    # The order expected 10,000 but only 9,000 was actually paid. Everything
    # AFTER payment correctly reflects the 9,000 that actually happened —
    # this proves the tracer correctly names PAYMENT as the root, not
    # settlement or bank, even though a naive glance at settlement/bank in
    # isolation would show them "matching" (their own inputs).
    order = make_order(amount=10_000)
    payment = make_payment(amount=9_000)
    settlement = make_settlement(settled=9_000)
    bank = make_bank_txn(amount=9_000)

    trace = trace_chain(order, [payment], [], settlement, [bank])

    assert trace.first_divergence.stage == "payment"
    assert trace.first_divergence.delta_paisa == -1_000
    refund_stage, settlement_stage, bank_stage = trace.downstream_impact
    assert refund_stage.consistent and settlement_stage.consistent and bank_stage.consistent
    assert trace.total_downstream_delta_paisa == -1_000  # the shortfall persists, nothing new added


def test_duplicate_bank_credit_sums_both_transactions_and_flags_divergence():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-dupbank")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    found = False
    for gt in batch.ground_truth:
        if gt.true_root_cause != "duplicate_bank_credit":
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        assert len(bank_txns) == 2
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        assert trace.status == "diverged"
        assert trace.first_divergence.stage == "bank"
        assert trace.first_divergence.delta_paisa > 0  # roughly double the expected amount
        found = True
        break
    assert found, "expected at least one accepted-and-matched duplicate_bank_credit case in this seed's data"


# --- genuinely unresolved divergence --------------------------------------------------


def test_missing_bank_transaction_is_unresolved_hand_crafted():
    order = make_order(amount=10_000)
    payment = make_payment(amount=10_000)
    settlement = make_settlement(settled=10_000)

    trace = trace_chain(order, [payment], [], settlement, bank_txns=[])

    assert trace.status == "unresolved"
    assert trace.first_divergence.stage == "bank"
    assert trace.first_divergence.actual_paisa is None
    assert trace.first_divergence.delta_paisa is None
    assert not trace.first_divergence.consistent
    assert trace.downstream_impact == []  # bank is the terminal stage
    assert trace.total_downstream_delta_paisa is None


def test_unresolvable_missing_bank_from_real_generated_data():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-div-unresolved")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    found = False
    for gt in batch.ground_truth:
        if gt.true_root_cause != "unknown":
            continue
        built = _build_trace_inputs(batch, result, gt.record_id)
        if built is None:
            continue
        order, payments, refunds, settlement, group_payments, group_refunds, bank_txns = built
        assert bank_txns == []
        trace = trace_chain(order, payments, refunds, settlement, bank_txns,
                             settlement_group_payments=group_payments, settlement_group_refunds=group_refunds)
        assert trace.status == "unresolved"
        assert trace.first_divergence.stage == "bank"
        found = True
        break
    assert found, "expected at least one accepted-and-matched unresolvable_missing_bank case in this seed's data"


# --- evidence ------------------------------------------------------------------------


def test_evidence_references_the_records_used():
    order = make_order(order_id="ord_X", amount=10_000)
    payment = make_payment(payment_id="pay_X", order_id="ord_X", amount=10_000)
    settlement = make_settlement(settlement_id="stl_X", settled=10_000)
    bank = make_bank_txn(bank_txn_id="bnk_X", amount=10_000)

    trace = trace_chain(order, [payment], [], settlement, [bank])

    payment_stage = next(s for s in trace.stages if s.stage == "payment")
    assert "ord_X" in payment_stage.evidence and "pay_X" in payment_stage.evidence
    settlement_stage = next(s for s in trace.stages if s.stage == "settlement")
    assert "stl_X" in settlement_stage.evidence and "pay_X" in settlement_stage.evidence
    bank_stage = next(s for s in trace.stages if s.stage == "bank")
    assert "stl_X" in bank_stage.evidence and "bnk_X" in bank_stage.evidence


# --- shared helper -----------------------------------------------------------------


def _build_trace_inputs(batch, result, order_id: str):
    """Mirrors what a real orchestrator would assemble from the matcher's
    accepted output for one order's case: the order's own payments/refunds,
    the settlement (and its full accepted payment/refund group) it matched
    into, and whatever bank transaction(s) were accepted for that
    settlement. Returns None if there's no accepted settlement match for
    this order (not this milestone's concern — that's a NO_MATCH case)."""
    order = next(o for o in batch.orders if o.order_id == order_id)
    payments = [p for p in batch.payments if p.order_id == order_id]
    payment_ids = {p.payment_id for p in payments}
    refunds = [r for r in batch.refunds if r.payment_id in payment_ids]

    accepted_settlement = [m for m in result.settlement_payment if m.accepted]
    settlement_id = next((m.target_id for m in accepted_settlement if m.source_id in payment_ids), None)
    if settlement_id is None:
        return None

    settlement = next(s for s in batch.settlements if s.settlement_id == settlement_id)
    group_payment_ids = {m.source_id for m in accepted_settlement if m.target_id == settlement_id}
    group_payments = [p for p in batch.payments if p.payment_id in group_payment_ids]
    group_refunds = [r for r in batch.refunds if r.payment_id in group_payment_ids]

    accepted_bank = [m for m in result.settlement_bank if m.accepted and m.source_id == settlement_id]
    bank_ids = {m.target_id for m in accepted_bank}
    bank_txns = [b for b in batch.bank_transactions if b.bank_txn_id in bank_ids]

    return order, payments, refunds, settlement, group_payments, group_refunds, bank_txns
