#!/usr/bin/env python
"""EVALUATION-ONLY experiment: does an AI root-cause investigator (Arm B)
materially improve on the deterministic known-cause rule table (Arm A)?

Population: every order whose settlement was successfully MATCHED but
FAILED verification (app.divergence.tracer.trace_chain status=="diverged")
- the real DIVERGENCE_TRACE population per section 6's state machine.
"unresolved" (missing bank evidence) and "clean" cases are excluded: there
is no root cause to investigate in either. partial_settlement_split cases
never reach this population at all (no accepted settlement match - see
milestone 3/6 notes); that is expected, not a gap in this experiment.

Arm A: app.pipeline.known_causes.detect_known_cause, UNCHANGED.
Arm B: a reference-quality stand-in for root_cause_investigator (no
ANTHROPIC_API_KEY in this environment). Deliberately built as a SUPERSET of
Arm A's own numeric logic PLUS narration-hint reading for the causes
Arm A structurally cannot reach (Settlement has no narration field) - the
most charitable fair test of what AI could plausibly add. Reads only the
narration text and evidence a real investigator would receive per section
10's input contract (divergence_stage, expected, actual, delta, evidence)
- NEVER reads ground truth.

Both arms' proposals are checked through
app.verifier.checks.verify_root_cause_proposal, UNCHANGED - the same gate
production code would use. Ground truth is read ONLY to score correctness
here, never to inform either arm's decision (same read-only-for-comparison
exception every other scripts/run_*.py report uses).

IMPORTANT METHODOLOGICAL NOTE (read before trusting "resolved" as "correct"):
section 10's own input contract hands the investigator the exact `delta`
value. Any arm that sets claimed_adjustment_paisa = delta (both Arm A's
rules and Arm B's stand-in do, since that is the natural, expected thing
to do when the gap is already given to you) makes
verify_root_cause_proposal's arithmetic-coverage check TRUE BY
CONSTRUCTION, regardless of which cause label is attached to it. The
verifier therefore CANNOT discriminate a correct label from a plausible
wrong one that shares the same delta - "passes verification" and "is the
correct root cause" are different claims. Only ground truth can check the
second one, which is the entire point of this experiment.

Does NOT modify app.matcher / app.verifier / app.divergence / app.pipeline
/ app.narration / app.datagen. Arm B lives only in this script.

Usage:
    python scripts/run_rootcause_validation.py --pool
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datagen.generator import generate_dataset  # noqa: E402
from app.datagen.models import GenBankTransaction, GenRefund, GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.divergence.tracer import trace_chain  # noqa: E402
from app.divergence.types import StageResult  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.pipeline.assemble import assemble_case_inputs  # noqa: E402
from app.pipeline.known_causes import detect_known_cause  # noqa: E402
from app.verifier.checks import verify_root_cause_proposal  # noqa: E402
from app.verifier.types import RootCauseProposal  # noqa: E402


# --- Arm B: reference-quality AI stand-in --------------------------------------


def simulate_ai_root_cause(
    first_divergence: StageResult, group_refunds: list[GenRefund], bank_txns: list[GenBankTransaction],
) -> RootCauseProposal | None:
    if first_divergence.delta_paisa is None:
        return None
    delta = first_divergence.delta_paisa
    settlement_id = first_divergence.evidence[0] if first_divergence.evidence else None
    narration_text = " ".join((b.narration or "") for b in bank_txns).upper()

    # Structured-evidence reasoning: mirrors known_causes.py's own numeric
    # logic. A competent investigator given the same refund evidence could
    # plausibly do this arithmetic too - included so Arm B is a fair
    # superset of Arm A, not a strictly different (and thus incomparable)
    # capability.
    if first_divergence.stage == "settlement":
        for r in group_refunds:
            if delta == r.amount_paisa:
                return RootCauseProposal("missing_refund_netting", delta, 0.85, [r.refund_id])
            if delta == -r.amount_paisa:
                return RootCauseProposal("duplicate_refund", delta, 0.85, [r.refund_id])
        if 0 < abs(delta) <= 5:
            return RootCauseProposal("currency_rounding", delta, 0.80, [settlement_id] if settlement_id else [])

    # Narration-hint reasoning: the capability Arm A structurally cannot
    # have (Settlement carries no narration field to check against - see
    # milestone 3 notes). This is Arm B's one genuinely new capability.
    if "PROC CHG" in narration_text or "ADDL" in narration_text:
        return RootCauseProposal("unreported_fee", delta, 0.85, [b.bank_txn_id for b in bank_txns])
    if "BANK CHARGES" in narration_text or "NET OF" in narration_text:
        return RootCauseProposal("unmatched_external_deduction", delta, 0.85, [b.bank_txn_id for b in bank_txns])
    if "(DUP)" in narration_text:
        return RootCauseProposal("duplicate_bank_credit", delta, 0.90, [b.bank_txn_id for b in bank_txns])
    if "ADJ" in narration_text:
        # A genuinely vague hint. A well-calibrated investigator should be
        # UNCONFIDENT here, not guess confidently — deliberately below the
        # section 10 gate (0.60), so this correctly escalates rather than
        # resolving on a weak signal.
        return RootCauseProposal("unreported_fee", delta, 0.45, [b.bank_txn_id for b in bank_txns])

    return None


def known_ids_for(inputs) -> set[str]:
    ids = {inputs.order.order_id}
    ids.update(p.payment_id for p in inputs.payments)
    ids.update(r.refund_id for r in inputs.refunds)
    ids.update(p.payment_id for p in inputs.settlement_group_payments)
    ids.update(r.refund_id for r in inputs.settlement_group_refunds)
    ids.update(b.bank_txn_id for b in inputs.bank_txns)
    if inputs.settlement is not None:
        ids.add(inputs.settlement.settlement_id)
    return ids


# --- experiment core -----------------------------------------------------------


@dataclass
class CaseRecord:
    order_id: str
    dataset: str
    true_cause: str | None
    is_ambiguous: bool
    a_resolved: bool
    a_correct: bool
    b_resolved: bool
    b_correct: bool


def run_experiment(batch: GeneratedBatch, gt_rows: list[GroundTruth], dataset_label: str) -> list[CaseRecord]:
    orders, payments, refunds, settlements, bank_txns = (
        batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions
    )
    gt_by_order = {g.record_id: g for g in gt_rows}
    result = run_deterministic_matching(orders, payments, refunds, settlements, bank_txns)

    records: list[CaseRecord] = []
    for order in orders:
        inputs = assemble_case_inputs(batch, result, order.order_id)
        if inputs.settlement is None or inputs.settlement_match is None:
            continue  # NO_MATCH population - not this experiment's concern

        trace = trace_chain(
            inputs.order, inputs.payments, inputs.refunds, inputs.settlement, inputs.bank_txns,
            settlement_group_payments=inputs.settlement_group_payments or inputs.payments,
            settlement_group_refunds=inputs.settlement_group_refunds or inputs.refunds,
        )
        if trace.status != "diverged":
            continue  # "clean" (nothing to investigate) or "unresolved" (no evidence to investigate)

        gt = gt_by_order.get(order.order_id)
        true_cause = gt.true_root_cause if gt else None
        is_ambiguous = bool(gt and gt.is_ambiguous)

        group_refunds = inputs.settlement_group_refunds or inputs.refunds
        known_ids = known_ids_for(inputs)

        a_proposal = detect_known_cause(trace.first_divergence, group_refunds, inputs.bank_txns)
        a_resolved, a_cause = False, None
        if a_proposal is not None:
            a_verify = verify_root_cause_proposal(
                a_proposal, trace.first_divergence.expected_paisa, trace.first_divergence.actual_paisa, known_ids,
            )
            if a_verify.passed:
                a_resolved, a_cause = True, a_proposal.root_cause

        b_proposal = simulate_ai_root_cause(trace.first_divergence, group_refunds, inputs.bank_txns)
        b_resolved, b_cause = False, None
        if b_proposal is not None:
            b_verify = verify_root_cause_proposal(
                b_proposal, trace.first_divergence.expected_paisa, trace.first_divergence.actual_paisa, known_ids,
            )
            if b_verify.passed:
                b_resolved, b_cause = True, b_proposal.root_cause

        records.append(CaseRecord(
            order_id=order.order_id, dataset=dataset_label, true_cause=true_cause, is_ambiguous=is_ambiguous,
            a_resolved=a_resolved, a_correct=bool(a_resolved and a_cause == true_cause),
            b_resolved=b_resolved, b_correct=bool(b_resolved and b_cause == true_cause),
        ))

    return records


# --- statistics ------------------------------------------------------------------


def exact_binomial_two_sided_p(k: int, n: int) -> float:
    if n == 0:
        return 1.0

    def pmf(i: int) -> float:
        return comb(n, i) * (0.5 ** n)

    pk = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= pk + 1e-12))


# --- reporting -------------------------------------------------------------------


def report(records: list[CaseRecord]) -> None:
    n = len(records)
    print(f"Total divergent (matched-but-failed-verification) cases: {n}")
    print()

    def summarize(arm: str, resolved_attr: str, correct_attr: str) -> None:
        resolved = sum(1 for r in records if getattr(r, resolved_attr))
        correct = sum(1 for r in records if getattr(r, correct_attr))
        false_res = resolved - correct
        escalated = n - resolved
        print(f"Arm {arm}:")
        print(f"  coverage (resolved):        {resolved}/{n} = {resolved / n:.1%}" if n else "  n=0")
        print(f"  escalation rate:             {escalated}/{n} = {escalated / n:.1%}" if n else "")
        print(f"  correct root-cause id rate:  {correct}/{n} = {correct / n:.1%}" if n else "")
        print(f"  false resolutions:           {false_res}/{n} = {false_res / n:.1%}" if n else "")
        print(f"  precision (of resolved):     {correct}/{resolved if resolved else 1} = {correct / resolved if resolved else 0:.1%}")
        print()

    summarize("A (deterministic rules)", "a_resolved", "a_correct")
    summarize("B (AI stand-in)", "b_resolved", "b_correct")

    both_correct = sum(1 for r in records if r.a_correct and r.b_correct)
    b_only_correct = sum(1 for r in records if r.b_correct and not r.a_correct)
    a_only_correct = sum(1 for r in records if r.a_correct and not r.b_correct)
    neither_correct = sum(1 for r in records if not r.a_correct and not r.b_correct)
    a_false = sum(1 for r in records if r.a_resolved and not r.a_correct)
    b_false = sum(1 for r in records if r.b_resolved and not r.b_correct)

    print("B vs A (paired, per case):")
    print(f"  both correct:        {both_correct}")
    print(f"  B correct, A wrong:  {b_only_correct}")
    print(f"  A correct, B wrong:  {a_only_correct}")
    print(f"  neither correct:     {neither_correct}")
    print(f"  A false resolutions: {a_false}")
    print(f"  B false resolutions: {b_false}")
    print()

    discordant = b_only_correct + a_only_correct
    if discordant > 0:
        p = exact_binomial_two_sided_p(b_only_correct, discordant)
        print(f"Sign test on discordant pairs (B-favoring vs A-favoring): "
              f"{b_only_correct}/{discordant} favor B, exact two-sided p={p:.4f}")
    else:
        print("No discordant pairs between A and B - cannot run a sign test (n=0).")
    print()

    print("Ambiguous cases (ground truth is_ambiguous=True) - correct behavior is to ESCALATE:")
    ambiguous = [r for r in records if r.is_ambiguous]
    for label, attr_resolved, attr_correct in (("A", "a_resolved", "a_correct"), ("B", "b_resolved", "b_correct")):
        resolved = sum(1 for r in ambiguous if getattr(r, attr_resolved))
        correct_escalations = len(ambiguous) - resolved
        print(f"  {label}: {len(ambiguous)} ambiguous cases -> {resolved} resolved anyway (confidently, possibly wrongly), "
              f"{correct_escalations} correctly escalated")
    print()

    print("Results by true root cause:")
    by_cause: dict[str, Counter] = {}
    for r in records:
        c = by_cause.setdefault(r.true_cause or "(none)", Counter())
        c["n"] += 1
        c["a_correct"] += int(r.a_correct)
        c["a_false"] += int(r.a_resolved and not r.a_correct)
        c["b_correct"] += int(r.b_correct)
        c["b_false"] += int(r.b_resolved and not r.b_correct)
    for cause, c in sorted(by_cause.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {cause:32s} n={c['n']:3d}  A: {c['a_correct']:2d} correct / {c['a_false']:2d} false   "
              f"B: {c['b_correct']:2d} correct / {c['b_false']:2d} false")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--pool", action="store_true",
                         help="run dev-v1 + heldout-v1 + three generated batches (seeds 42/7/99) and pool all results")
    args = parser.parse_args()

    all_records: list[CaseRecord] = []

    if args.pool:
        print("=== Milestone 8 pre-validation: root-cause Arm A vs Arm B, POOLED across 5 datasets ===")
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
            print(f"dataset: {dv}  (orders={len(batch.orders)})")
            all_records.extend(run_experiment(batch, gt_rows, dv))
        for seed, count in ((42, 500), (7, 400), (99, 400)):
            label = f"gen-seed{seed}-n{count}"
            batch = generate_dataset(seed=seed, num_flows=count, dataset_version=label)
            print(f"dataset: {label}  (orders={len(batch.orders)})")
            all_records.extend(run_experiment(batch, batch.ground_truth, label))
        print()
        report(all_records)
        return

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
        label = args.dataset_version
    else:
        label = f"gen-seed{args.seed}-n{args.count}"
        batch = generate_dataset(seed=args.seed, num_flows=args.count, dataset_version=label)
        gt_rows = batch.ground_truth

    print(f"=== Milestone 8 pre-validation: {label} ===")
    all_records = run_experiment(batch, gt_rows, label)
    report(all_records)


if __name__ == "__main__":
    main()
