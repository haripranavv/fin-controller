"""Top-level synthetic dataset generator.

generate_dataset(...) is the only function most callers need. Deterministic
given (seed, num_flows, dataset_version, reference_date) — same inputs
always produce byte-identical output (see
tests/test_datagen.py::test_reproducible_given_same_seed).

reference_date anchors the generated date window instead of wall-clock
"now" specifically so runs stay reproducible regardless of what day they're
actually run on.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

from app.datagen import catalog
from app.datagen import flows as flow_builders
from app.datagen import settlement as settlement_builders
from app.datagen.models import (
    AXIS_A_CLEAN,
    AXIS_A_DUPLICATE_REFERENCE,
    GeneratedBatch,
    OrderFlow,
    batch_id as make_batch_id,
)

DEFAULT_REFERENCE_DATE = date(2026, 8, 1)
DEFAULT_DATE_WINDOW_DAYS = 45
# Reserved fraction of flows applied to app.datagen.settlement's
# apply_partial_settlement_split out-of-band, bypassing normal per-merchant
# grouping (see that module's docstring). Only kicks in once there's enough
# volume that reserving a few flows doesn't distort the rest of the mix.
PARTIAL_SPLIT_FRACTION = 0.03
PARTIAL_SPLIT_MIN_FLOWS = 20


def generate_dataset(
    *,
    seed: int,
    num_flows: int,
    dataset_version: str,
    reference_date: date = DEFAULT_REFERENCE_DATE,
) -> GeneratedBatch:
    if num_flows < 4:
        raise ValueError("num_flows must be at least 4")

    rng = random.Random(seed)

    num_split = 0
    if num_flows >= PARTIAL_SPLIT_MIN_FLOWS:
        num_split = max(1, round(num_flows * PARTIAL_SPLIT_FRACTION))
    num_normal = num_flows - num_split

    categories = rng.choices(
        list(flow_builders.AXIS_A_WEIGHTS), weights=list(flow_builders.AXIS_A_WEIGHTS.values()), k=num_normal
    )

    # Pair up duplicate-reference flows so two genuinely share a token
    # (computed up front so the pairing itself doesn't depend on draw order
    # inside the main build loop below).
    dup_indices = [i for i, c in enumerate(categories) if c == AXIS_A_DUPLICATE_REFERENCE]
    dup_token_by_index: dict[int, str] = {}
    for a, b in zip(dup_indices[0::2], dup_indices[1::2]):
        shared = f"{rng.randint(10000, 99999)}"
        dup_token_by_index[a] = shared
        dup_token_by_index[b] = shared

    window_end = datetime.combine(reference_date, time(23, 59), tzinfo=timezone.utc)
    window_start = window_end - timedelta(days=DEFAULT_DATE_WINDOW_DAYS)
    window_seconds = int((window_end - window_start).total_seconds())

    def _random_created_at() -> datetime:
        return window_start + timedelta(seconds=rng.randint(0, window_seconds))

    order_flows: list[OrderFlow] = []
    for i in range(num_normal):
        merchant_id, merchant_name = rng.choice(catalog.MERCHANTS)
        flow = flow_builders.build_order_flow(
            rng=rng,
            dataset_version=dataset_version,
            idx=i + 1,
            merchant_id=merchant_id,
            merchant_name=merchant_name,
            created_at=_random_created_at(),
            category=categories[i],
            shared_ref_token=dup_token_by_index.get(i),
        )
        order_flows.append(flow)

    split_flows: list[OrderFlow] = []
    for j in range(num_split):
        merchant_id, merchant_name = rng.choice(catalog.MERCHANTS)
        flow = flow_builders.build_order_flow(
            rng=rng,
            dataset_version=dataset_version,
            idx=num_normal + j + 1,
            merchant_id=merchant_id,
            merchant_name=merchant_name,
            created_at=_random_created_at(),
            category=AXIS_A_CLEAN,
        )
        split_flows.append(flow)

    all_flows = order_flows + split_flows
    split_flow_order_ids = {f.order.order_id for f in split_flows}

    settlements, bank_txns, gt_by_order = settlement_builders.assign_settlements(
        rng=rng,
        dataset_version=dataset_version,
        flows=all_flows,
        split_flow_order_ids=split_flow_order_ids,
    )

    batch = GeneratedBatch(batch_id=make_batch_id(dataset_version), dataset_version=dataset_version, seed=seed)
    for f in all_flows:
        batch.orders.append(f.order)
        batch.payments.extend(f.payments)
        batch.refunds.extend(f.refunds)  # includes any refund injected during settlement assignment
    batch.settlements = settlements
    batch.bank_transactions = bank_txns
    batch.ground_truth = [gt_by_order[f.order.order_id] for f in all_flows]

    return batch
