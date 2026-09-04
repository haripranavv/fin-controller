"""Groups order flows into settlements per merchant (PROJECT_SPEC.md section
8.4: "a settlement can represent multiple payments") and, for a minority of
settlement groups, applies a deliberate axis-B financial divergence scenario
— one per PROJECT_SPEC.md section 10 root cause, plus an "unknown"/missing-
evidence case and a genuinely ambiguous one. See app/datagen/models.py for
the axis A/B split and the AXIS_B_* string constants.

Which scenarios are "deterministically explainable" vs "genuinely need AI
investigation" (informing the divergence engine's future "known cause" rule
table — see docs/ARCHITECTURE_NOTES.md item 3) is a deliberate design choice
here, not incidental:

- missing_refund_netting / duplicate_refund: delta exactly equals a known
  refund amount already in the Refund table, sign disambiguates the two
  (settlement too high vs too low) — a pure numeric rule, no AI needed.
- currency_rounding: |delta| is tiny (1-5 paisa) — a magnitude-only rule.
- duplicate_bank_credit: structurally detectable (two bank transactions
  referencing the same settlement/date/amount pattern).
- partial_settlement_split: not injected here at all — see generator.py,
  which reserves a slice of flows and calls apply_partial_settlement_split
  directly, bypassing normal grouping. Deterministically explainable IF the
  matcher widens its settlement search, per section 8.4.
- unreported_fee / unmatched_external_deduction: NOT numerically derivable
  from any other record — a bank narration hint is the only clue, so these
  genuinely require narration/evidence-driven AI investigation.
- ambiguous_cause: delta sits in a gray zone (bigger than a rounding delta,
  smaller than a typical fee) with a deliberately vague narration hint —
  legitimately supports more than one explanation.
- unresolvable_missing_bank: the bank leg is omitted entirely. No amount of
  investigation can safely resolve this from within the batch — correct
  behavior is ESCALATED, root cause "unknown".
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.datagen import catalog
from app.datagen.models import (
    AXIS_A_CLEAN,
    AXIS_B_AMBIGUOUS_CAUSE,
    AXIS_B_CURRENCY_ROUNDING,
    AXIS_B_DUPLICATE_BANK_CREDIT,
    AXIS_B_DUPLICATE_REFUND,
    AXIS_B_MISSING_REFUND_NETTING,
    AXIS_B_NONE,
    AXIS_B_PARTIAL_SETTLEMENT_SPLIT,
    AXIS_B_UNMATCHED_EXTERNAL_DEDUCTION,
    AXIS_B_UNREPORTED_FEE,
    AXIS_B_UNRESOLVABLE_MISSING_BANK,
    GenBankTransaction,
    GenGroundTruth,
    GenRefund,
    GenSettlement,
    OrderFlow,
    bank_txn_id as make_bank_txn_id,
    refund_id as make_refund_id,
    settlement_id as make_settlement_id,
)

AXIS_B_WEIGHTS: dict[str, int] = {
    AXIS_B_NONE: 55,
    AXIS_B_UNREPORTED_FEE: 6,
    AXIS_B_MISSING_REFUND_NETTING: 6,
    AXIS_B_DUPLICATE_REFUND: 5,
    AXIS_B_CURRENCY_ROUNDING: 6,
    AXIS_B_DUPLICATE_BANK_CREDIT: 5,
    AXIS_B_UNMATCHED_EXTERNAL_DEDUCTION: 5,
    AXIS_B_UNRESOLVABLE_MISSING_BANK: 6,
    AXIS_B_AMBIGUOUS_CAUSE: 6,
}

MAX_GROUP_SIZE = 5

# Flows for one merchant are scattered uniformly across the whole
# generation window (no time-locality by design — see generator.py), so
# without bucketing first, chunking a merchant's flows into groups of
# consecutive-by-date items can still produce a settlement whose period
# spans several weeks purely by chance. Real settlement cycles are short
# and regular; capping the bucket width keeps generated periods realistic
# and keeps merchant-adjacent settlements' date windows from overlapping
# heavily (which was confusing the matcher's date-based candidate
# filtering in milestone 3 — see docs/ARCHITECTURE_NOTES.md).
SETTLEMENT_CYCLE_DAYS = 7.0


class _Counter:
    def __init__(self, start: int = 1) -> None:
        self._n = start

    def next(self) -> int:
        n = self._n
        self._n += 1
        return n


def assign_settlements(
    rng: random.Random,
    dataset_version: str,
    flows: list[OrderFlow],
    split_flow_order_ids: set[str],
) -> tuple[list[GenSettlement], list[GenBankTransaction], dict[str, GenGroundTruth]]:
    """Group non-reserved flows by merchant into settlement batches, apply
    an axis-B scenario to some groups, and build the partial-settlement-split
    pairs for the reserved flows. Returns (settlements, bank_transactions,
    ground_truth_by_order_id) covering every flow passed in."""
    settlements: list[GenSettlement] = []
    bank_txns: list[GenBankTransaction] = []
    gt_by_order: dict[str, GenGroundTruth] = {}

    settlement_counter = _Counter()
    bank_counter = _Counter()

    normal_flows = [f for f in flows if f.order.order_id not in split_flow_order_ids]
    split_flows = [f for f in flows if f.order.order_id in split_flow_order_ids]

    by_merchant: dict[str, list[OrderFlow]] = {}
    for f in normal_flows:
        by_merchant.setdefault(f.order.merchant_id, []).append(f)
    for merchant_flows in by_merchant.values():
        merchant_flows.sort(key=lambda f: f.order.created_at)

    for merchant_id, merchant_flows in by_merchant.items():
        chunks: list[list[OrderFlow]] = []
        for bucket in _bucket_by_cycle(merchant_flows):
            i = 0
            while i < len(bucket):
                size = rng.randint(1, min(MAX_GROUP_SIZE, len(bucket) - i))
                chunks.append(bucket[i : i + size])
                i += size

        for group in _merge_non_positive_chunks(chunks):
            _apply_settlement_group(
                rng, dataset_version, settlement_counter, bank_counter, merchant_id, group,
                settlements, bank_txns, gt_by_order,
            )

    for f in split_flows:
        _apply_partial_settlement_split(
            rng, dataset_version, settlement_counter, bank_counter, f, settlements, bank_txns, gt_by_order
        )

    return settlements, bank_txns, gt_by_order


def _bucket_by_cycle(merchant_flows: list[OrderFlow], cycle_days: float = SETTLEMENT_CYCLE_DAYS) -> list[list[OrderFlow]]:
    """Split time-sorted flows (already sorted by order.created_at by the
    caller) into buckets spanning at most `cycle_days`, so a settlement's
    period stays realistically short."""
    buckets: list[list[OrderFlow]] = []
    current: list[OrderFlow] = []
    bucket_start: datetime | None = None
    for f in merchant_flows:
        if current and (f.order.created_at - bucket_start) >= timedelta(days=cycle_days):
            buckets.append(current)
            current = []
            bucket_start = None
        if not current:
            bucket_start = f.order.created_at
        current.append(f)
    if current:
        buckets.append(current)
    return buckets


def _merge_non_positive_chunks(chunks: list[list[OrderFlow]]) -> list[list[OrderFlow]]:
    """A lone (or mostly) refund_full flow nets to a small NEGATIVE
    contribution (gross - refund == -(fee+tax) exactly), which is realistic
    per-transaction but nonsensical as a standalone settlement total.
    Fold any chunk whose combined net contribution isn't positive into an
    adjacent one before axis-B is even considered."""
    merged: list[list[OrderFlow]] = []
    for chunk in chunks:
        if merged and sum(f.net_contribution_paisa for f in chunk) <= 0:
            merged[-1].extend(chunk)
        else:
            merged.append(list(chunk))
    if len(merged) >= 2 and sum(f.net_contribution_paisa for f in merged[0]) <= 0:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def _pick_or_inject_refund(rng: random.Random, group: list[OrderFlow], dataset_version: str) -> GenRefund:
    """Return an existing refund from the group if one exists, else inject a
    new one onto a random member's first payment (mutates that flow)."""
    candidates = [f for f in group if f.refunds]
    if candidates:
        flow = rng.choice(candidates)
        return rng.choice(flow.refunds)

    flow = rng.choice(group)
    payment = flow.payments[0]
    amount = round(payment.amount_paisa * rng.uniform(0.2, 0.45))
    refund = GenRefund(
        refund_id=make_refund_id(dataset_version, flow.flow_idx, suffix="_inj"),
        payment_id=payment.payment_id,
        amount_paisa=amount,
        reason_code=rng.choice(catalog.REFUND_REASON_CODES),
        narration=f"REFUND {payment.payment_id}",
        created_at=payment.created_at + timedelta(days=rng.randint(1, 5)),
    )
    flow.refunds.append(refund)
    return refund


def _apply_settlement_group(
    rng: random.Random,
    dataset_version: str,
    settlement_counter: _Counter,
    bank_counter: _Counter,
    merchant_id: str,
    group: list[OrderFlow],
    settlements: list[GenSettlement],
    bank_txns: list[GenBankTransaction],
    gt_by_order: dict[str, GenGroundTruth],
) -> None:
    scenario = rng.choices(list(AXIS_B_WEIGHTS), weights=list(AXIS_B_WEIGHTS.values()), k=1)[0]

    stage: str | None = None
    root_cause: str | None = None
    ambiguous = False
    applied_scenario: str | None = None  # what actually got applied, for injected_noise_type
    bank_narration_suffix = ""
    bank_delta = 0
    include_bank = True
    make_duplicate_bank_txn = False

    refund_target: GenRefund | None = None
    if scenario in (AXIS_B_MISSING_REFUND_NETTING, AXIS_B_DUPLICATE_REFUND):
        refund_target = _pick_or_inject_refund(rng, group, dataset_version)

    # Computed AFTER any refund injection above, so a freshly-injected
    # refund is already correctly netted into the "no divergence applied"
    # baseline (net_contribution_paisa is a live property).
    baseline = sum(f.net_contribution_paisa for f in group)
    settled_amount = baseline

    # Every scenario that subtracts from an amount is guarded against
    # producing a non-positive settlement/bank figure (small groups, or
    # groups already carrying heavy refunds, can otherwise go negative —
    # caught by test_settlement_amounts_are_always_positive against a real
    # generated batch). A guard failing means the group falls back to
    # "none": no divergence recorded, but any refund _pick_or_inject_refund
    # already injected stays (harmlessly correctly netted into baseline).

    if scenario == AXIS_B_UNREPORTED_FEE:
        candidate = baseline - rng.randint(500, 4000)
        if candidate > 0:
            settled_amount = candidate
            stage, root_cause, applied_scenario = "settlement", "unreported_fee", scenario
            bank_narration_suffix = " ADDL PROC CHG APPLIED"

    elif scenario == AXIS_B_MISSING_REFUND_NETTING:
        assert refund_target is not None
        settled_amount = baseline + refund_target.amount_paisa
        stage, root_cause, applied_scenario = "settlement", "missing_refund_netting", scenario

    elif scenario == AXIS_B_DUPLICATE_REFUND:
        assert refund_target is not None
        candidate = baseline - refund_target.amount_paisa
        if candidate > 0:
            settled_amount = candidate
            stage, root_cause, applied_scenario = "settlement", "duplicate_refund", scenario

    elif scenario == AXIS_B_CURRENCY_ROUNDING:
        candidate = baseline - rng.randint(1, 5)
        if candidate > 0:
            settled_amount = candidate
            stage, root_cause, applied_scenario = "settlement", "currency_rounding", scenario

    elif scenario == AXIS_B_AMBIGUOUS_CAUSE:
        candidate = baseline - rng.randint(150, 450)
        if candidate > 0:
            settled_amount = candidate
            stage, root_cause, ambiguous, applied_scenario = "settlement", "unreported_fee", True, scenario
            bank_narration_suffix = " ADJ"

    elif scenario == AXIS_B_DUPLICATE_BANK_CREDIT:
        stage, root_cause, applied_scenario = "bank", "duplicate_bank_credit", scenario
        make_duplicate_bank_txn = True

    elif scenario == AXIS_B_UNMATCHED_EXTERNAL_DEDUCTION:
        candidate_delta = rng.randint(300, 2500)
        if baseline - candidate_delta > 0:
            bank_delta = candidate_delta
            stage, root_cause, applied_scenario = "bank", "unmatched_external_deduction", scenario
            bank_narration_suffix = " NET OF BANK CHARGES"

    elif scenario == AXIS_B_UNRESOLVABLE_MISSING_BANK:
        include_bank = False
        stage, root_cause, ambiguous, applied_scenario = "bank", "unknown", True, scenario

    period_start = min(f.order.created_at for f in group)
    period_end = max(p.created_at for f in group for p in f.payments) + timedelta(hours=rng.randint(1, 6))
    settle_created_at = period_end + timedelta(days=rng.randint(1, 3))
    bank_value_date = settle_created_at + timedelta(days=rng.randint(0, 2))

    sid = make_settlement_id(dataset_version, settlement_counter.next())
    fee_deducted = sum(p.fee_paisa + p.tax_on_fee_paisa for f in group for p in f.payments)

    # Defense in depth: _merge_non_positive_chunks handles the normal case
    # (a lone heavily-refunded flow), but a merchant with too few flows to
    # merge into could still reach here non-positive. Never emit a
    # non-positive settlement — clamp rather than violate the invariant.
    settled_amount = max(settled_amount, 1)

    settlements.append(
        GenSettlement(
            settlement_id=sid,
            merchant_id=merchant_id,
            settled_amount_paisa=settled_amount,
            fee_deducted_paisa=fee_deducted,
            period_start=period_start,
            period_end=period_end,
            created_at=settle_created_at,
        )
    )

    bank_ids: list[str] = []
    if include_bank:
        bank_amount = settled_amount - bank_delta
        main_bank_id = make_bank_txn_id(dataset_version, bank_counter.next())
        bank_txns.append(
            GenBankTransaction(
                bank_txn_id=main_bank_id,
                amount_paisa=bank_amount,
                value_date=bank_value_date,
                utr_ref=f"UTR{rng.randint(100000, 999999)}",
                narration=f"SETTLE/{merchant_id}/{sid}{bank_narration_suffix}",
            )
        )
        bank_ids.append(main_bank_id)

        if make_duplicate_bank_txn:
            dup_id = make_bank_txn_id(dataset_version, bank_counter.next(), suffix="_dup")
            bank_txns.append(
                GenBankTransaction(
                    bank_txn_id=dup_id,
                    amount_paisa=bank_amount,
                    value_date=bank_value_date,
                    utr_ref=f"UTR{rng.randint(100000, 999999)}DUP",
                    narration=f"SETTLE/{merchant_id}/{sid} (DUP)",
                )
            )
            bank_ids.append(dup_id)

    for f in group:
        parts = []
        if f.axis_a_category != AXIS_A_CLEAN:
            parts.append(f.axis_a_category)
        if applied_scenario:
            parts.append(applied_scenario)
        injected = "+".join(parts) if parts else AXIS_A_CLEAN

        gt_by_order[f.order.order_id] = GenGroundTruth(
            record_id=f.order.order_id,
            true_match_ids=f.match_ids + [sid] + bank_ids,
            true_divergence_stage=stage,
            true_root_cause=root_cause,
            is_ambiguous=ambiguous,
            injected_noise_type=injected,
        )


def _apply_partial_settlement_split(
    rng: random.Random,
    dataset_version: str,
    settlement_counter: _Counter,
    bank_counter: _Counter,
    flow: OrderFlow,
    settlements: list[GenSettlement],
    bank_txns: list[GenBankTransaction],
    gt_by_order: dict[str, GenGroundTruth],
) -> None:
    """One flow's net contribution is split across two separate settlement
    rows (and their own bank transactions), both fully present in the batch.
    Each settlement/bank pair is individually correct — the only "trick" is
    that fully explaining this payment requires combining both, so the
    initial (single-settlement) view under-covers it by design."""
    total = flow.net_contribution_paisa
    split_ratio = rng.uniform(0.4, 0.6)
    part1 = round(total * split_ratio)
    part2 = total - part1
    p0 = flow.payments[0]
    merchant_id = flow.order.merchant_id

    sid1 = make_settlement_id(dataset_version, settlement_counter.next())
    sid2 = make_settlement_id(dataset_version, settlement_counter.next())

    period1_end = p0.created_at + timedelta(days=rng.randint(1, 2))
    settle1_created_at = period1_end + timedelta(days=1)
    period2_start = period1_end + timedelta(days=1)
    period2_end = period2_start + timedelta(days=rng.randint(1, 2))
    settle2_created_at = period2_end + timedelta(days=1)

    settlements.append(
        GenSettlement(
            settlement_id=sid1, merchant_id=merchant_id, settled_amount_paisa=part1,
            fee_deducted_paisa=p0.fee_paisa + p0.tax_on_fee_paisa,
            period_start=p0.created_at, period_end=period1_end, created_at=settle1_created_at,
        )
    )
    settlements.append(
        GenSettlement(
            settlement_id=sid2, merchant_id=merchant_id, settled_amount_paisa=part2,
            fee_deducted_paisa=0,
            period_start=period2_start, period_end=period2_end, created_at=settle2_created_at,
        )
    )

    b1_id = make_bank_txn_id(dataset_version, bank_counter.next())
    b2_id = make_bank_txn_id(dataset_version, bank_counter.next())
    bank_txns.append(
        GenBankTransaction(
            bank_txn_id=b1_id, amount_paisa=part1, value_date=settle1_created_at + timedelta(days=1),
            utr_ref=f"UTR{rng.randint(100000, 999999)}", narration=f"SETTLE/{merchant_id}/{sid1}",
        )
    )
    bank_txns.append(
        GenBankTransaction(
            bank_txn_id=b2_id, amount_paisa=part2, value_date=settle2_created_at + timedelta(days=1),
            utr_ref=f"UTR{rng.randint(100000, 999999)}", narration=f"SETTLE/{merchant_id}/{sid2}",
        )
    )

    parts = [] if flow.axis_a_category == AXIS_A_CLEAN else [flow.axis_a_category]
    parts.append(AXIS_B_PARTIAL_SETTLEMENT_SPLIT)

    gt_by_order[flow.order.order_id] = GenGroundTruth(
        record_id=flow.order.order_id,
        true_match_ids=flow.match_ids + [sid1, sid2, b1_id, b2_id],
        true_divergence_stage="settlement",
        true_root_cause="partial_settlement_split",
        is_ambiguous=False,
        injected_noise_type="+".join(parts),
    )
