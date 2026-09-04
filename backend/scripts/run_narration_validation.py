#!/usr/bin/env python
"""EVALUATION-ONLY validation experiment for Milestone 7's revised AI claim
(see chat: "the strongest defensible framing... AI extracts identity clues
from narration and enables deterministic recovery of cases the original
matcher could not reach").

Three arms, run on the baseline-unmatched population only:
  A. Baseline       - app.matcher, unchanged. Defines the failure population.
  B. Blind widen     - the SAME date-window bypass mechanism as
                        app.narration.rematch.attempt_rematch, but with NO
                        extraction and NO AI at all: isolates "does
                        relaxing the filter alone explain any recovery"
                        from AI's actual contribution.
  C. AI-assisted      - app.narration.extractor + app.narration.rematch,
                        UNCHANGED, exactly as shipped in milestone 7.

Ground truth is used ONLY to score correctness of what B/C recover -
never to inform matching itself (same read-only-for-comparison exception
every other scripts/run_*.py report uses).

Does NOT modify app.matcher / app.narration / app.pipeline / app.datagen.
Arm B's "blind widen" function is defined only in this script, deliberately
NOT added to app.narration.rematch - this is a diagnostic experiment, not
a production change.

Usage (run from backend/, with the venv active):
    python scripts/run_narration_validation.py --dataset-version dev-v1
    python scripts/run_narration_validation.py --dataset-version heldout-v1
    python scripts/run_narration_validation.py --generate --seed 42 --count 500
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datagen import catalog  # noqa: E402
from app.datagen.generator import generate_dataset  # noqa: E402
from app.datagen.models import GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.matcher import subset_sum  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import (  # noqa: E402
    SETTLEMENT_DATE_WINDOW_SLACK_DAYS,
    compute_net_contributions,
    run_deterministic_matching,
)
from app.matcher.scoring import SETTLEMENT_MATCH_ACCEPT_THRESHOLD, amount_score, date_proximity_score
from app.matcher.types import MatchCandidate  # noqa: E402
from app.models.enums import MatchMethod, RecordType  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.narration.extractor import extract_narration  # noqa: E402
from app.narration.rematch import attempt_rematch  # noqa: E402
from app.narration.types import NarrationExtraction  # noqa: E402

# --- Arm B: blind widen, NO AI ------------------------------------------------
# Deliberately a standalone copy of attempt_rematch's widening mechanism
# MINUS extraction/confidence-gate/amount-hint-guard - lives only in this
# evaluation script, not in app.narration, so it can never accidentally
# become a production code path.


def blind_widen_rematch(
    target_payment_id: str, orders, payments, refunds, candidate_settlements, already_consumed_payment_ids: set[str],
) -> MatchCandidate | None:
    contributions = compute_net_contributions(payments, refunds, orders)
    contributions_by_id = {c.payment_id: c for c in contributions}
    target = contributions_by_id.get(target_payment_id)
    if target is None:
        return None

    best_candidate: MatchCandidate | None = None
    best_score = 0.0
    for settlement in candidate_settlements:
        if settlement.merchant_id != target.merchant_id:
            continue
        pool = [
            c for c in contributions
            if c.merchant_id == settlement.merchant_id
            and c.payment_id not in already_consumed_payment_ids
            and c.payment_id != target_payment_id
            and date_proximity_score(c.created_at, settlement.period_start, settlement.period_end, decay_days=SETTLEMENT_DATE_WINDOW_SLACK_DAYS) > 0
        ]
        pool.append(target)
        if len(pool) > subset_sum.MAX_ITEMS:
            continue
        items = [(c.payment_id, c.net_contribution_paisa) for c in pool]
        best = subset_sum.closest_subset_sums(items, settlement.settled_amount_paisa, k=1)[0]
        if target_payment_id not in best.member_ids:
            continue
        score = amount_score(best.delta, settlement.settled_amount_paisa)
        if score >= SETTLEMENT_MATCH_ACCEPT_THRESHOLD and score > best_score:
            best_score = score
            best_candidate = MatchCandidate(
                source_type=RecordType.PAYMENT.value, source_id=target_payment_id,
                target_type=RecordType.SETTLEMENT.value, target_id=settlement.settlement_id,
                method=MatchMethod.FUZZY_CANDIDATE.value, score=round(score, 4), accepted=True,
            )
    return best_candidate


# --- Arm C's extractor: reference stand-in (no ANTHROPIC_API_KEY here) -----

_TOKEN_TO_MERCHANT = {catalog.token(name): name for _mid, name in catalog.MERCHANTS}


def _reference_extract_client():
    class _Client:
        def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
            import json

            payload = json.loads(user_prompt)
            narration = payload["narration"]
            upper = narration.upper() if narration else ""
            counterparty = next((name for token, name in _TOKEN_TO_MERCHANT.items() if token in upper), None)
            ref_match = re.search(r"(?:INV|REF)\s*([A-Z0-9]+)", upper)
            reference_id = ref_match.group(1) if ref_match else None
            amount_hint = None
            imps_match = re.match(r"^IMPS:[A-Z0-9]+:[A-Z0-9]+:(\d+)$", upper)
            if imps_match:
                amount_hint = int(imps_match.group(1)) * 100
            flags = ["partial"] if "PARTIAL" in upper else []
            confidence = 0.85 if counterparty else 0.55
            extraction = NarrationExtraction(
                counterparty=counterparty, reference_id=reference_id, amount_hint=amount_hint,
                transaction_type="payment", flags=flags, confidence=confidence,
            )
            return extraction.model_dump_json()

    return _Client()


# --- failure-cause classification (evaluation-only, ground truth) -------------


def classify_failure_cause(order_id: str, gt_by_order: dict, reports_by_settlement: dict, true_settlement_id: str | None) -> str:
    gt = gt_by_order.get(order_id)
    if gt and gt.true_root_cause == "partial_settlement_split":
        return "partial_settlement_split"
    if true_settlement_id is None:
        return "no_ground_truth_settlement"
    report = reports_by_settlement.get(true_settlement_id)
    if report is None:
        return "settlement_not_processed"
    return {
        "no_match": "no_candidates_or_below_threshold",
        "too_many_candidates": "too_many_candidates",
        "ambiguous": "ambiguous_tie",
        "matched": "excluded_from_own_settlement",
    }.get(report.outcome, f"unknown({report.outcome})")


# --- experiment core -----------------------------------------------------------


@dataclass
class CaseRecord:
    payment_id: str
    order_id: str
    dataset: str
    cause: str
    b_recovered: bool
    b_correct: bool
    c_recovered: bool
    c_correct: bool


def run_experiment(batch: GeneratedBatch, gt_rows: list[GroundTruth], dataset_label: str) -> list[CaseRecord]:
    orders, payments, refunds, settlements, bank_txns = (
        batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions
    )
    gt_by_order = {g.record_id: g for g in gt_rows}
    payment_to_order = {p.payment_id: p.order_id for p in payments}

    arm_a = run_deterministic_matching(orders, payments, refunds, settlements, bank_txns)
    accepted_a = [m for m in arm_a.settlement_payment if m.accepted]
    consumed_a = {m.source_id for m in accepted_a}
    reports_by_settlement = {r.subject_id: r for r in arm_a.settlement_payment_reports}

    def true_settlement_ids(order_id: str) -> set[str]:
        # partial_settlement_split carries TWO true settlement ids (both
        # halves) — checking membership in the full set, not equality to
        # an arbitrarily-first one, is required for a fair correctness
        # check on those cases.
        gt = gt_by_order.get(order_id)
        if not gt:
            return set()
        return {mid for mid in gt.true_match_ids if mid.startswith("stl_")}

    unmatched = sorted((p for p in payments if p.payment_id not in consumed_a), key=lambda p: p.payment_id)

    consumed_b = set(consumed_a)
    consumed_c = set(consumed_a)
    client = _reference_extract_client()

    records: list[CaseRecord] = []
    for p in unmatched:
        order_id = payment_to_order[p.payment_id]
        true_stls = true_settlement_ids(order_id)
        cause = classify_failure_cause(order_id, gt_by_order, reports_by_settlement, next(iter(true_stls), None))

        b_result = blind_widen_rematch(p.payment_id, orders, payments, refunds, settlements, consumed_b)
        b_recovered = b_result is not None
        b_correct = bool(b_recovered and b_result.target_id in true_stls)
        if b_recovered:
            consumed_b.add(p.payment_id)

        c_recovered = False
        c_correct = False
        if p.narration:
            outcome = extract_narration(client, p.narration, p.amount_paisa, p.created_at.date().isoformat())
            if outcome.error is None and outcome.passed_confidence_gate:
                c_result = attempt_rematch(
                    p.payment_id, outcome.extraction, orders, payments, refunds, settlements,
                    already_consumed_payment_ids=consumed_c,
                )
                c_recovered = c_result is not None
                c_correct = bool(c_recovered and c_result.target_id in true_stls)
                if c_recovered:
                    consumed_c.add(p.payment_id)

        records.append(CaseRecord(
            payment_id=p.payment_id, order_id=order_id, dataset=dataset_label, cause=cause,
            b_recovered=b_recovered, b_correct=b_correct, c_recovered=c_recovered, c_correct=c_correct,
        ))

    return records


# --- statistics ------------------------------------------------------------------


def exact_binomial_two_sided_p(k: int, n: int) -> float:
    """Exact two-sided sign-test p-value for k successes out of n trials
    under p=0.5 (a pure-Python McNemar-style test on discordant pairs, no
    scipy dependency)."""
    if n == 0:
        return 1.0

    def pmf(i: int) -> float:
        return comb(n, i) * (0.5 ** n)

    pk = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= pk + 1e-12))


# --- reporting -------------------------------------------------------------------


def report(records: list[CaseRecord]) -> None:
    n = len(records)
    print(f"Total baseline-unmatched payments across all datasets: {n}")
    print()

    def summarize(arm: str, recovered_attr: str, correct_attr: str) -> None:
        recovered = sum(1 for r in records if getattr(r, recovered_attr))
        correct = sum(1 for r in records if getattr(r, correct_attr))
        false_matches = recovered - correct
        recovery_rate = recovered / n if n else 0.0
        correct_recovery_rate = correct / n if n else 0.0
        false_match_rate = false_matches / recovered if recovered else 0.0
        print(f"Arm {arm}:")
        print(f"  recovery rate:         {recovered}/{n} = {recovery_rate:.1%}")
        print(f"  correct recovery rate: {correct}/{n} = {correct_recovery_rate:.1%}")
        print(f"  false-match rate (of recovered): {false_matches}/{recovered if recovered else 1} = {false_match_rate:.1%}")
        print()

    print(f"Arm A (baseline): recovers 0/{n} by definition - this population IS baseline's failure set.")
    print()
    summarize("B (blind widen, no AI)", "b_recovered", "b_correct")
    summarize("C (AI-assisted)", "c_recovered", "c_correct")

    # C vs B — paired comparison
    both_correct = sum(1 for r in records if r.b_correct and r.c_correct)
    c_only_correct = sum(1 for r in records if r.c_correct and not r.b_correct)
    b_only_correct = sum(1 for r in records if r.b_correct and not r.c_correct)
    neither_correct = sum(1 for r in records if not r.b_correct and not r.c_correct)
    b_false_matches = sum(1 for r in records if r.b_recovered and not r.b_correct)
    c_false_matches = sum(1 for r in records if r.c_recovered and not r.c_correct)

    print("C vs B (paired, per payment):")
    print(f"  both correct:        {both_correct}")
    print(f"  C correct, B wrong:  {c_only_correct}")
    print(f"  B correct, C wrong:  {b_only_correct}")
    print(f"  neither correct:     {neither_correct}")
    print(f"  B false matches (recovered but wrong): {b_false_matches}")
    print(f"  C false matches (recovered but wrong): {c_false_matches}")
    print()

    discordant = c_only_correct + b_only_correct
    if discordant > 0:
        p_value = exact_binomial_two_sided_p(c_only_correct, discordant)
        print(f"Sign test on discordant pairs (C-favoring vs B-favoring): "
              f"{c_only_correct}/{discordant} favor C, exact two-sided p={p_value:.4f}")
    else:
        print("No discordant pairs between B and C - cannot run a sign test (n=0).")
    print()

    print("Results by failure cause:")
    by_cause: dict[str, Counter] = {}
    for r in records:
        c = by_cause.setdefault(r.cause, Counter())
        c["n"] += 1
        c["b_correct"] += int(r.b_correct)
        c["b_false"] += int(r.b_recovered and not r.b_correct)
        c["c_correct"] += int(r.c_correct)
        c["c_false"] += int(r.c_recovered and not r.c_correct)
    for cause, c in sorted(by_cause.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {cause:32s} n={c['n']:3d}  B: {c['b_correct']:2d} correct / {c['b_false']:2d} false   "
              f"C: {c['c_correct']:2d} correct / {c['c_false']:2d} false")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version", help="load a persisted dataset from Postgres")
    parser.add_argument("--generate", action="store_true", help="generate an in-memory dataset instead")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--pool", action="store_true",
                         help="run dev-v1 + heldout-v1 + three generated batches (seeds 42/7/99) and pool all results")
    args = parser.parse_args()

    all_records: list[CaseRecord] = []

    if args.pool:
        print("=== Milestone 7 validation experiment: POOLED across 5 datasets ===")
        for dv in ("dev-v1", "heldout-v1"):
            session = SessionLocal()
            gt_session = GroundTruthSessionLocal()
            try:
                orders, payments, refunds, settlements, bank_txns = load_dataset(session, dv)
                gt_rows = gt_session.query(GroundTruth).filter(GroundTruth.record_id.like(f"%_{dv}_%")).all()
            finally:
                session.close()
                gt_session.close()
            batch = GeneratedBatch(batch_id=f"b_{dv}", dataset_version=dv, seed=0,
                                    orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)
            print(f"dataset: {dv}  (orders={len(batch.orders)} payments={len(batch.payments)})")
            all_records.extend(run_experiment(batch, gt_rows, dv))
        for seed, count in ((42, 500), (7, 400), (99, 400)):
            label = f"gen-seed{seed}-n{count}"
            batch = generate_dataset(seed=seed, num_flows=count, dataset_version=label)
            print(f"dataset: {label}  (orders={len(batch.orders)} payments={len(batch.payments)})")
            all_records.extend(run_experiment(batch, batch.ground_truth, label))
        print()
        report(all_records)
        return

    datasets: list[tuple[str, GeneratedBatch, list[GroundTruth]]] = []

    if args.dataset_version:
        session = SessionLocal()
        gt_session = GroundTruthSessionLocal()
        try:
            orders, payments, refunds, settlements, bank_txns = load_dataset(session, args.dataset_version)
            gt_rows = gt_session.query(GroundTruth).filter(GroundTruth.record_id.like(f"%_{args.dataset_version}_%")).all()
        finally:
            session.close()
            gt_session.close()
        batch = GeneratedBatch(batch_id=f"b_{args.dataset_version}", dataset_version=args.dataset_version, seed=0,
                                orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)
        datasets.append((args.dataset_version, batch, gt_rows))
    else:
        label = f"gen-seed{args.seed}-n{args.count}"
        batch = generate_dataset(seed=args.seed, num_flows=args.count, dataset_version=label)
        datasets.append((label, batch, batch.ground_truth))

    print("=== Milestone 7 validation experiment: Arms A / B / C ===")
    for label, batch, gt_rows in datasets:
        print(f"dataset: {label}  (orders={len(batch.orders)} payments={len(batch.payments)})")
        records = run_experiment(batch, gt_rows, label)
        all_records.extend(records)
    print()

    report(all_records)


if __name__ == "__main__":
    main()
