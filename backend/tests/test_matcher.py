"""Milestone 3 tests: the deterministic matcher's pure logic. No database —
exercises app.matcher directly, using app.datagen.generator's in-memory
datasets as realistic fixtures (same technique milestone 2 used for its own
tests).
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.datagen.generator import generate_dataset
from app.matcher import normalize
from app.matcher.reconciler import (
    match_orders_payments,
    match_payments_refunds,
    match_settlement_bank,
    match_settlement_payments,
    run_deterministic_matching,
)
from app.matcher.scoring import amount_score, date_proximity_score
from app.matcher.subset_sum import MAX_ITEMS, closest_subset_sums

MATCHER_DIR = Path(__file__).resolve().parent.parent / "app" / "matcher"


# --- isolation -----------------------------------------------------------


def test_matcher_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in MATCHER_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


# --- normalize.py ------------------------------------------------------------


def test_normalize_text_collapses_punctuation_and_case():
    assert normalize.normalize_text("NEFT-HDFC-RAJTRADCO") == "NEFT HDFC RAJTRADCO"
    assert normalize.normalize_text("  messy   spacing  ") == "MESSY SPACING"
    assert normalize.normalize_text(None) == ""


def test_normalize_reference_strips_known_prefix():
    assert normalize.normalize_reference("INV-88213") == "88213"
    assert normalize.normalize_reference("ref1234") == "1234"
    assert normalize.normalize_reference("88213") == "88213"


def test_contains_reference_survives_punctuation_differences():
    haystack = "SETTLE/mch_0008/stl_dev-v1_00039 ADDL PROC CHG APPLIED"
    assert normalize.contains_reference(haystack, "stl_dev-v1_00039")
    assert not normalize.contains_reference(haystack, "stl_dev-v1_00040")
    assert not normalize.contains_reference(None, "x")
    assert not normalize.contains_reference("x", "")


# --- scoring.py ------------------------------------------------------------


def test_amount_score_exact_and_decay():
    assert amount_score(0, 10_000) == 1.0
    assert amount_score(10_000, 10_000) == 0.0  # delta equals the full target
    assert amount_score(5_000, 10_000) == pytest.approx(0.5)  # half the target
    assert 0.0 < amount_score(2_500, 10_000) < 1.0
    # small target floor: ₹5 scale even for a ₹1 target
    assert amount_score(100, 100) > 0.0


def test_date_proximity_inside_window_is_perfect():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert date_proximity_score(datetime(2026, 1, 3, tzinfo=timezone.utc), start, end) == 1.0


def test_date_proximity_decays_outside_window():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 5, tzinfo=timezone.utc)
    just_after = end + timedelta(days=1)
    far_after = end + timedelta(days=20)
    assert 0.0 < date_proximity_score(just_after, start, end, decay_days=10) < 1.0
    assert date_proximity_score(far_after, start, end, decay_days=10) == 0.0


# --- subset_sum.py -----------------------------------------------------------


def test_closest_subset_sums_finds_exact_match():
    items = [("a", 100), ("b", 250), ("c", 400)]
    results = closest_subset_sums(items, target=350, k=3)
    assert results[0].delta == 0
    assert set(results[0].member_ids) == {"a", "b"}


def test_closest_subset_sums_finds_best_fit_when_no_exact_match():
    items = [("a", 100), ("b", 300)]
    # No subset hits 390 exactly: {} = 0, {a} = 100, {b} = 300, {a,b} = 400.
    # {a,b} is closest (delta +10) even though it overshoots.
    results = closest_subset_sums(items, target=390, k=3)
    assert results[0].member_ids == ("a", "b")
    assert results[0].delta == 10


def test_closest_subset_sums_includes_empty_subset_when_best():
    items = [("a", 5000)]
    results = closest_subset_sums(items, target=1, k=3)
    assert results[0].member_ids == ()
    assert results[0].total == 0


def test_closest_subset_sums_detects_a_tie():
    # Two disjoint pairs summing to the same total -> both should surface.
    items = [("a", 100), ("b", 200), ("c", 150), ("d", 150)]
    results = closest_subset_sums(items, target=300, k=3)
    totals = {r.total for r in results}
    assert 300 in totals  # a+b and c+d both hit it exactly


def test_subset_sum_raises_over_max_items():
    items = [(str(i), i) for i in range(MAX_ITEMS + 1)]
    with pytest.raises(ValueError):
        closest_subset_sums(items, target=10)


# --- reconciler.py: exact FK legs ------------------------------------------


@pytest.fixture(scope="module")
def batch():
    return generate_dataset(seed=42, num_flows=180, dataset_version="test-matcher")


def test_order_payment_always_matches(batch):
    matches = match_orders_payments(batch.orders, batch.payments)
    assert len(matches) == len(batch.payments)
    assert all(m.accepted and m.score == 1.0 for m in matches)


def test_payment_refund_always_matches(batch):
    matches = match_payments_refunds(batch.payments, batch.refunds)
    assert len(matches) == len(batch.refunds)
    assert all(m.accepted and m.score == 1.0 for m in matches)


# --- reconciler.py: settlement <-> payment and <-> bank ---------------------


def _ground_truth_by_order(batch):
    return {g.record_id: g for g in batch.ground_truth}


def test_clean_settlements_are_recovered_exactly(batch):
    """For every flow whose ground truth carries no root cause (a genuinely
    clean settlement group), the matcher's accepted settlement-payment and
    settlement-bank matches must reproduce the true match set exactly."""
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    accepted_settlement_targets = {(m.source_id, m.target_id) for m in result.settlement_payment if m.accepted}
    accepted_bank_targets = {(m.source_id, m.target_id) for m in result.settlement_bank if m.accepted}
    payment_to_order = {p.payment_id: p.order_id for p in batch.payments}

    gt_by_order = _ground_truth_by_order(batch)
    checked = 0
    for p_id, o_id in payment_to_order.items():
        gt = gt_by_order[o_id]
        if gt.true_root_cause is not None:
            continue  # only clean flows here

        predicted_settlements = {t for (s, t) in accepted_settlement_targets if s == p_id}
        true_settlements = {mid for mid in gt.true_match_ids if mid.startswith("stl_")}
        if not true_settlements:
            continue  # this payment's flow doesn't anchor the settlement check (e.g. no refund-only flow)
        assert predicted_settlements == true_settlements, f"{p_id}: predicted {predicted_settlements} != true {true_settlements}"

        true_banks = {mid for mid in gt.true_match_ids if mid.startswith("bnk_")}
        for sid in true_settlements:
            predicted_banks = {t for (s, t) in accepted_bank_targets if s == sid}
            assert predicted_banks == true_banks, f"{sid}: predicted {predicted_banks} != true {true_banks}"
        checked += 1

    assert checked > 10


def test_unresolvable_missing_bank_yields_no_bank_match(batch):
    gt_by_order = _ground_truth_by_order(batch)
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    accepted_bank_sources = {m.source_id for m in result.settlement_bank if m.accepted}

    unresolved_settlement_ids = set()
    for g in gt_by_order.values():
        if g.true_root_cause == "unknown":
            unresolved_settlement_ids.update(mid for mid in g.true_match_ids if mid.startswith("stl_"))
    assert unresolved_settlement_ids

    for sid in unresolved_settlement_ids:
        assert sid not in accepted_bank_sources, f"{sid} should have no bank match (none exists)"


def test_bank_reference_match_beats_fallback_for_duplicate_bank_credit(batch):
    """duplicate_bank_credit settlements have two real bank txns referencing
    them; the matcher should find both via exact reference, not silently
    pick just one via the fallback path."""
    gt_by_order = _ground_truth_by_order(batch)
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    accepted_bank = [m for m in result.settlement_bank if m.accepted]

    dup_settlement_ids = set()
    for g in gt_by_order.values():
        if g.true_root_cause == "duplicate_bank_credit":
            dup_settlement_ids.update(mid for mid in g.true_match_ids if mid.startswith("stl_"))
    assert dup_settlement_ids

    for sid in dup_settlement_ids:
        matches_for_sid = [m for m in accepted_bank if m.source_id == sid]
        assert len(matches_for_sid) == 2
        assert all(m.method == "exact_reference" and m.score == 1.0 for m in matches_for_sid)


def test_no_payment_is_reused_across_accepted_settlement_matches(batch):
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    accepted = [m for m in result.settlement_payment if m.accepted]
    seen: set[str] = set()
    for m in accepted:
        assert m.source_id not in seen, f"{m.source_id} matched to more than one settlement"
        seen.add(m.source_id)


def test_settlement_payment_reports_cover_every_settlement(batch):
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    reported_ids = {r.subject_id for r in result.settlement_payment_reports}
    assert reported_ids == {s.settlement_id for s in batch.settlements}
