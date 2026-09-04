#!/usr/bin/env python
"""Milestone 3 verification report: run the deterministic matcher over a
persisted dataset and compare its output against ground truth.

This is NOT the real evaluation harness (that's milestone 10 — baseline vs
AI-enhanced, EvaluationRun rows, held-out reporting discipline). It's a
standalone, read-only diagnostic script for verifying the matcher itself,
which is why it's the one place outside app.datagen.persist allowed to read
ground truth: app.matcher's own code never does (see app/matcher/__init__.py).

Usage (run from backend/, with the venv active, Postgres up and migrated):
    python scripts/run_matcher_report.py --dataset-version dev-v1
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402


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

    print(f"=== Milestone 3 matcher report: {dv} ===")
    print(f"orders={len(orders)} payments={len(payments)} refunds={len(refunds)} "
          f"settlements={len(settlements)} bank_txns={len(bank_txns)} ground_truth={len(ground_truth)}")
    print()

    result = run_deterministic_matching(orders, payments, refunds, settlements, bank_txns)

    print(f"Order<->Payment matches:  {len(result.order_payment)}/{len(payments)} (should always be 100%)")
    print(f"Payment<->Refund matches: {len(result.payment_refund)}/{len(refunds)} (should always be 100%)")
    print()

    outcome_counts = Counter(r.outcome for r in result.settlement_payment_reports)
    print("Settlement<->Payment stage outcomes:", dict(outcome_counts))
    bank_outcome_counts = Counter(r.outcome for r in result.settlement_bank_reports)
    print("Settlement<->Bank stage outcomes:   ", dict(bank_outcome_counts))
    print()

    # --- accuracy vs ground truth, by axis-A category and by root cause ---
    gt_by_order = {g.record_id: g for g in ground_truth}
    payment_to_order = {p.payment_id: p.order_id for p in payments}
    payments_by_order: dict[str, list[str]] = {}
    for p in payments:
        payments_by_order.setdefault(p.order_id, []).append(p.payment_id)

    accepted_settlement = {(m.source_id, m.target_id) for m in result.settlement_payment if m.accepted}
    accepted_bank = {(m.source_id, m.target_id) for m in result.settlement_bank if m.accepted}

    def predicted_match_ids(order_id: str) -> set[str]:
        ids: set[str] = set()
        for pid in payments_by_order.get(order_id, []):
            ids.add(pid)  # order<->payment is always a trivial exact match
            for r in refunds:
                if r.payment_id == pid:
                    ids.add(r.refund_id)
            for (s, t) in accepted_settlement:
                if s == pid:
                    ids.add(t)
                    for (s2, t2) in accepted_bank:
                        if s2 == t:
                            ids.add(t2)
        return ids

    # injected_noise_type is "axis_a+axis_b" when both apply (see
    # app/datagen/models.py) — grouping by its first "+"-segment alone hides
    # axis-B (root cause) outcomes whenever an axis-A category is also
    # present. Report both breakdowns.
    by_axis_a: dict[str, list[bool]] = {}
    by_root_cause: dict[str, list[bool]] = {}
    total_true_ids = 0
    total_correct_ids = 0
    total_predicted_ids = 0
    exact_flow_matches = 0
    total_flows_with_targets = 0

    for order_id, gt in gt_by_order.items():
        true_ids = set(gt.true_match_ids)
        predicted_ids = predicted_match_ids(order_id)

        total_true_ids += len(true_ids)
        total_predicted_ids += len(predicted_ids)
        total_correct_ids += len(true_ids & predicted_ids)

        exact = predicted_ids == true_ids
        by_axis_a.setdefault(gt.injected_noise_type.split("+")[0], []).append(exact)
        if gt.true_root_cause:
            by_root_cause.setdefault(gt.true_root_cause, []).append(exact)
        if true_ids:
            total_flows_with_targets += 1
            if exact:
                exact_flow_matches += 1

    print(f"Exact flow-level match (predicted == true match_ids): {exact_flow_matches}/{total_flows_with_targets} "
          f"= {exact_flow_matches / total_flows_with_targets:.1%}")
    precision = total_correct_ids / total_predicted_ids if total_predicted_ids else 0.0
    recall = total_correct_ids / total_true_ids if total_true_ids else 0.0
    print(f"ID-level precision: {precision:.1%}  recall: {recall:.1%}")
    print()

    print("Exact flow-level match rate by axis-A category (first segment of injected_noise_type):")
    for category, results in sorted(by_axis_a.items()):
        rate = sum(results) / len(results)
        print(f"  {category:30s} {sum(results):4d}/{len(results):<4d} = {rate:.1%}")
    print()

    print("Exact flow-level match rate by true root cause (axis-B, regardless of axis-A prefix):")
    for cause, results in sorted(by_root_cause.items()):
        rate = sum(results) / len(results)
        print(f"  {cause:30s} {sum(results):4d}/{len(results):<4d} = {rate:.1%}")


if __name__ == "__main__":
    main()
