"""Milestone 2 tests: the synthetic data generator's pure logic. No database
needed — these exercise app.datagen.generator directly against the
dataclass representation, not the persisted rows (persistence is verified
manually against live Postgres; see docs/ARCHITECTURE_NOTES.md).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.datagen.generator import generate_dataset
from app.datagen.models import AXIS_A_CLEAN

DATAGEN_DIR = Path(__file__).resolve().parent.parent / "app" / "datagen"


# --- isolation -----------------------------------------------------------


def test_datagen_does_not_import_groundtruth_outside_persist():
    """Every module in app.datagen except persist.py must be import-clean of
    the isolated ground-truth session/model (see app/datagen/__init__.py)."""
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in DATAGEN_DIR.glob("*.py"):
        if path.name in ("persist.py", "__init__.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


# --- reproducibility -------------------------------------------------------


def test_reproducible_given_same_seed():
    a = generate_dataset(seed=42, num_flows=60, dataset_version="test-repro")
    b = generate_dataset(seed=42, num_flows=60, dataset_version="test-repro")
    assert a.orders == b.orders
    assert a.payments == b.payments
    assert a.refunds == b.refunds
    assert a.settlements == b.settlements
    assert a.bank_transactions == b.bank_transactions
    assert a.ground_truth == b.ground_truth


def test_different_seed_gives_different_output():
    a = generate_dataset(seed=1, num_flows=60, dataset_version="test-seed-a")
    b = generate_dataset(seed=2, num_flows=60, dataset_version="test-seed-b")
    assert [o.amount_paisa for o in a.orders] != [o.amount_paisa for o in b.orders]


# --- referential integrity / money invariants -------------------------------


@pytest.fixture(scope="module")
def batch():
    return generate_dataset(seed=42, num_flows=180, dataset_version="test-batch")


def test_every_payment_references_a_real_order(batch):
    order_ids = {o.order_id for o in batch.orders}
    for p in batch.payments:
        assert p.order_id in order_ids


def test_every_refund_references_a_real_payment(batch):
    payment_ids = {p.payment_id for p in batch.payments}
    for r in batch.refunds:
        assert r.payment_id in payment_ids


def test_order_amount_equals_sum_of_its_payments(batch):
    payments_by_order: dict[str, int] = {}
    for p in batch.payments:
        payments_by_order[p.order_id] = payments_by_order.get(p.order_id, 0) + p.amount_paisa
    for o in batch.orders:
        assert o.amount_paisa == payments_by_order[o.order_id]


def test_all_monetary_fields_are_int(batch):
    for o in batch.orders:
        assert isinstance(o.amount_paisa, int)
    for p in batch.payments:
        assert isinstance(p.amount_paisa, int)
        assert isinstance(p.fee_paisa, int)
        assert isinstance(p.tax_on_fee_paisa, int)
    for r in batch.refunds:
        assert isinstance(r.amount_paisa, int)
    for s in batch.settlements:
        assert isinstance(s.settled_amount_paisa, int)
    for b in batch.bank_transactions:
        assert isinstance(b.amount_paisa, int)


def test_settlement_amounts_are_always_positive(batch):
    # Guards the duplicate_refund scenario's non-positive fallback (see
    # settlement.py) actually works across a large, seeded run.
    for s in batch.settlements:
        assert s.settled_amount_paisa > 0


def test_no_duplicate_ids_within_a_type(batch):
    assert len({o.order_id for o in batch.orders}) == len(batch.orders)
    assert len({p.payment_id for p in batch.payments}) == len(batch.payments)
    assert len({r.refund_id for r in batch.refunds}) == len(batch.refunds)
    assert len({s.settlement_id for s in batch.settlements}) == len(batch.settlements)
    assert len({b.bank_txn_id for b in batch.bank_transactions}) == len(batch.bank_transactions)


# --- ground truth ------------------------------------------------------------


def test_ground_truth_has_exactly_one_row_per_order(batch):
    order_ids = {o.order_id for o in batch.orders}
    gt_ids = [g.record_id for g in batch.ground_truth]
    assert set(gt_ids) == order_ids
    assert len(gt_ids) == len(set(gt_ids))


def test_ground_truth_match_ids_reference_real_records(batch):
    all_ids = (
        {p.payment_id for p in batch.payments}
        | {r.refund_id for r in batch.refunds}
        | {s.settlement_id for s in batch.settlements}
        | {b.bank_txn_id for b in batch.bank_transactions}
    )
    for g in batch.ground_truth:
        for match_id in g.true_match_ids:
            assert match_id in all_ids, f"{match_id} in {g.record_id}'s true_match_ids doesn't exist"


def test_ground_truth_root_cause_is_from_the_bounded_spec_enum(batch):
    # PROJECT_SPEC.md section 10's closed set.
    allowed = {
        "duplicate_refund", "missing_refund_netting", "unreported_fee",
        "partial_settlement_split", "currency_rounding", "duplicate_bank_credit",
        "unmatched_external_deduction", "unknown",
    }
    seen = {g.true_root_cause for g in batch.ground_truth if g.true_root_cause is not None}
    assert seen.issubset(allowed)


def test_all_axis_a_categories_appear(batch):
    seen = {g.injected_noise_type.split("+")[0] for g in batch.ground_truth}
    # Every axis-A category name should show up as the first "+"-segment of
    # at least one flow's injected_noise_type, at this sample size/seed.
    expected = {
        AXIS_A_CLEAN, "messy_narration", "duplicate_reference", "delayed_event",
        "partial_payment", "refund_partial", "refund_full",
    }
    assert expected.issubset(seen | {"unreported_fee", "missing_refund_netting", "duplicate_refund",
                                      "currency_rounding", "duplicate_bank_credit",
                                      "unmatched_external_deduction", "unknown", "partial_settlement_split"})


def test_at_least_one_of_every_root_cause_appears(batch):
    # At num_flows=180 with the configured weights this is a reproducible
    # (fixed seed) property, not a flaky one.
    seen = {g.true_root_cause for g in batch.ground_truth if g.true_root_cause}
    expected = {
        "duplicate_refund", "missing_refund_netting", "unreported_fee",
        "partial_settlement_split", "currency_rounding", "duplicate_bank_credit",
        "unmatched_external_deduction", "unknown",
    }
    missing = expected - seen
    assert not missing, f"root causes never generated at this seed/count: {missing}"


def test_unresolvable_cases_are_flagged_ambiguous_with_no_bank_match(batch):
    unresolved = [g for g in batch.ground_truth if g.true_root_cause == "unknown"]
    assert unresolved
    bank_ids = {b.bank_txn_id for b in batch.bank_transactions}
    for g in unresolved:
        assert g.is_ambiguous is True
        assert not any(mid in bank_ids for mid in g.true_match_ids)


# --- settlement math consistency --------------------------------------------


def test_clean_settlements_exactly_equal_recomputed_baseline(batch):
    """For every settlement whose member flows carry no divergence label,
    settled_amount_paisa must exactly equal the sum of those flows'
    net_contribution_paisa recomputed from the underlying payment/refund
    records — i.e. the generator's own "expected" formula actually holds
    for the cases it claims are clean."""
    payments_by_id = {p.payment_id: p for p in batch.payments}
    refunds_by_payment: dict[str, list] = {}
    for r in batch.refunds:
        refunds_by_payment.setdefault(r.payment_id, []).append(r)

    settlements_by_id = {s.settlement_id: s for s in batch.settlements}
    orders_by_payment_via_gt: dict[str, str] = {}  # settlement_id -> set of member order record_ids

    members_by_settlement: dict[str, list] = {}
    for g in batch.ground_truth:
        if g.true_root_cause is not None:
            continue  # only check clean settlements here
        for match_id in g.true_match_ids:
            if match_id in settlements_by_id:
                members_by_settlement.setdefault(match_id, []).append(g)

    payments_by_order: dict[str, list] = {}
    for p in batch.payments:
        payments_by_order.setdefault(p.order_id, []).append(p)

    checked = 0
    for sid, member_gts in members_by_settlement.items():
        total = 0
        for g in member_gts:
            order_id = g.record_id
            for p in payments_by_order.get(order_id, []):
                total += p.amount_paisa - p.fee_paisa - p.tax_on_fee_paisa
                for r in refunds_by_payment.get(p.payment_id, []):
                    total -= r.amount_paisa
        assert total == settlements_by_id[sid].settled_amount_paisa, sid
        checked += 1
    assert checked > 10  # sanity: the recomputation actually ran on a meaningful sample


def test_missing_refund_netting_delta_equals_a_refund_amount(batch):
    _assert_settlement_delta_matches_refund(batch, "missing_refund_netting", expect_sign=+1)


def test_duplicate_refund_delta_equals_a_refund_amount(batch):
    _assert_settlement_delta_matches_refund(batch, "duplicate_refund", expect_sign=-1)


def _settlement_group_baseline_and_refunds(batch, settlement_id: str) -> tuple[int, list[int]]:
    """Recompute a settlement's *whole group* expected total (every flow
    whose true_match_ids names this settlement, not just one) plus the list
    of individual refund amounts across that group — a settlement can cover
    several flows (PROJECT_SPEC.md section 8.4), so a delta check against
    only one member's baseline is wrong whenever the group size is > 1."""
    refunds_by_payment: dict[str, list] = {}
    for r in batch.refunds:
        refunds_by_payment.setdefault(r.payment_id, []).append(r)
    payments_by_order: dict[str, list] = {}
    for p in batch.payments:
        payments_by_order.setdefault(p.order_id, []).append(p)

    member_order_ids = [g.record_id for g in batch.ground_truth if settlement_id in g.true_match_ids]
    baseline = 0
    refund_amounts: list[int] = []
    for order_id in member_order_ids:
        for p in payments_by_order.get(order_id, []):
            baseline += p.amount_paisa - p.fee_paisa - p.tax_on_fee_paisa
            for r in refunds_by_payment.get(p.payment_id, []):
                baseline -= r.amount_paisa
                refund_amounts.append(r.amount_paisa)
    return baseline, refund_amounts


def _assert_settlement_delta_matches_refund(batch, root_cause: str, expect_sign: int) -> None:
    settlements_by_id = {s.settlement_id: s for s in batch.settlements}
    matches = [g for g in batch.ground_truth if g.true_root_cause == root_cause]
    assert matches
    seen_settlements: set[str] = set()
    for g in matches:
        sid = next(mid for mid in g.true_match_ids if mid in settlements_by_id)
        if sid in seen_settlements:
            continue  # a group's other members produce the same (sid, delta) pair
        seen_settlements.add(sid)
        baseline, refund_amounts = _settlement_group_baseline_and_refunds(batch, sid)
        delta = settlements_by_id[sid].settled_amount_paisa - baseline
        assert any(delta == expect_sign * amt for amt in refund_amounts), (
            f"{sid}: delta {delta} doesn't match any refund amount {refund_amounts} * {expect_sign}"
        )


def test_currency_rounding_delta_is_small(batch):
    settlements_by_id = {s.settlement_id: s for s in batch.settlements}
    matches = [g for g in batch.ground_truth if g.true_root_cause == "currency_rounding"]
    assert matches
    seen_settlements: set[str] = set()
    for g in matches:
        sid = next(mid for mid in g.true_match_ids if mid in settlements_by_id)
        if sid in seen_settlements:
            continue
        seen_settlements.add(sid)
        baseline, _ = _settlement_group_baseline_and_refunds(batch, sid)
        delta = baseline - settlements_by_id[sid].settled_amount_paisa
        assert 0 < delta <= 5


def test_partial_settlement_split_settlements_sum_to_expected(batch):
    settlements_by_id = {s.settlement_id: s for s in batch.settlements}
    payments_by_order: dict[str, list] = {}
    for p in batch.payments:
        payments_by_order.setdefault(p.order_id, []).append(p)

    matches = [g for g in batch.ground_truth if g.true_root_cause == "partial_settlement_split"]
    assert matches
    for g in matches:
        settlement_ids = [mid for mid in g.true_match_ids if mid in settlements_by_id]
        assert len(settlement_ids) == 2
        combined = sum(settlements_by_id[sid].settled_amount_paisa for sid in settlement_ids)
        expected = sum(p.amount_paisa - p.fee_paisa - p.tax_on_fee_paisa for p in payments_by_order[g.record_id])
        assert combined == expected
