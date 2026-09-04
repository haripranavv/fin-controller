#!/usr/bin/env python
"""Milestone 6 verification report: run the deterministic end-to-end
pipeline over a persisted dataset and report RESOLVED/ESCALATED outcomes,
cross-tabulated against ground truth.

Like scripts/run_matcher_report.py, this is a standalone diagnostic script,
not the real evaluation harness (milestone 10). It is the one place outside
app.datagen.persist allowed to read ground truth, and only for comparison
output — app.pipeline itself never does (see app/pipeline/__init__.py).

Usage (run from backend/, with the venv active, Postgres up and a dataset
generated):
    python scripts/run_pipeline_report.py --dataset-version dev-v1
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datagen.models import GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.pipeline.assemble import assemble_case_inputs  # noqa: E402
from app.pipeline.pipeline import resolve_case  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    dv = args.dataset_version

    session = SessionLocal()
    gt_session = GroundTruthSessionLocal()
    try:
        orders, payments, refunds, settlements, bank_txns = load_dataset(session, dv)
        ground_truth = gt_session.query(GroundTruth).filter(GroundTruth.record_id.like(f"%_{dv}_%")).all()
    finally:
        session.close()
        gt_session.close()

    batch = GeneratedBatch(
        batch_id=f"batch_{dv}", dataset_version=dv, seed=0,
        orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns,
    )
    matcher_result = run_deterministic_matching(orders, payments, refunds, settlements, bank_txns)
    gt_by_order = {g.record_id: g for g in ground_truth}

    print(f"=== Milestone 6 pipeline report: {dv} ===")
    print(f"orders={len(orders)}  payments={len(payments)}  settlements={len(settlements)}  bank_txns={len(bank_txns)}")
    print()

    outcomes = Counter()
    by_true_cause: dict[str, Counter] = {}
    escalation_reasons = Counter()

    for order in orders:
        inputs = assemble_case_inputs(batch, matcher_result, order.order_id)
        result = resolve_case(inputs)
        outcomes[result.outcome] += 1

        gt = gt_by_order.get(order.order_id)
        cause_label = gt.true_root_cause if (gt and gt.true_root_cause) else "clean"
        by_true_cause.setdefault(cause_label, Counter())[result.outcome] += 1

        if result.outcome == "ESCALATED":
            # first few words of the reason, as a rough bucket
            bucket = result.reason.split(":")[1].strip().split(" ")[0] if ":" in result.reason else result.reason
            escalation_reasons[bucket] += 1

    total = sum(outcomes.values())
    print(f"RESOLVED:  {outcomes['RESOLVED']:4d} / {total} = {outcomes['RESOLVED'] / total:.1%}")
    print(f"ESCALATED: {outcomes['ESCALATED']:4d} / {total} = {outcomes['ESCALATED'] / total:.1%}")
    print()

    print("Outcome by true root cause (ground truth, for verification only):")
    for cause, counter in sorted(by_true_cause.items()):
        n = sum(counter.values())
        print(f"  {cause:30s} RESOLVED={counter['RESOLVED']:4d}  ESCALATED={counter['ESCALATED']:4d}  (n={n})")
    print()

    print("Escalation reason buckets:")
    for bucket, n in escalation_reasons.most_common():
        print(f"  {bucket:20s} {n}")


if __name__ == "__main__":
    main()
