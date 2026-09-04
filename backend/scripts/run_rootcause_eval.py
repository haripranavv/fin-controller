#!/usr/bin/env python
"""Milestone 8 real-AI evaluation: same methodology as
scripts/run_rootcause_validation.py (the pre-implementation experiment
that justified building this milestone), but Arm B now runs through the
REAL production code path — app.rootcause.case.investigate_case and
app.verifier.checks.verify_root_cause_proposal, both UNCHANGED — instead
of a standalone simulation function.

Uses the REAL Anthropic API if ANTHROPIC_API_KEY is set (via
app.rootcause.client.AnthropicRootCauseClient), otherwise falls back to a
rule-based stand-in client and says so clearly in the output — this script
must be runnable and honest either way. The stand-in reuses the same
narration-hint logic validated in run_rootcause_validation.py, wrapped
behind the RootCauseLLMClient protocol so it exercises the real
investigate_root_cause()/investigate_case() code (schema validation,
confidence gate, verifier call) — only the "model" itself is simulated.

Ground truth is used ONLY to score correctness here, never to inform
either arm's decision (same read-only-for-comparison exception every
other scripts/run_*.py report uses). Does NOT modify app.matcher /
app.verifier / app.divergence / app.pipeline / app.rootcause / app.datagen.

Usage:
    python scripts/run_rootcause_eval.py --pool
    python scripts/run_rootcause_eval.py --dataset-version dev-v1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.datagen.generator import generate_dataset  # noqa: E402
from app.datagen.models import GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.divergence.tracer import trace_chain  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.pipeline.assemble import assemble_case_inputs  # noqa: E402
from app.pipeline.known_causes import detect_known_cause  # noqa: E402
from app.rootcause.case import investigate_case  # noqa: E402
from app.rootcause.client import AnthropicRootCauseClient, GeminiRootCauseClient, RootCauseLLMClient  # noqa: E402
from app.verifier.checks import verify_root_cause_proposal  # noqa: E402


# --- stand-in client (used only when no ANTHROPIC_API_KEY) --------------------


class _ReferenceStandInClient:
    """Wraps the same rule-based reasoning validated in
    run_rootcause_validation.py behind RootCauseLLMClient, so the REAL
    investigate_root_cause()/investigate_case() production path (schema
    validation, confidence gate, evidence citation) runs even without a
    live API key. Never reads ground truth."""

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = json.loads(user_prompt)
        stage = payload["divergence_stage"]
        delta = payload["delta"]
        evidence = payload["evidence"]
        narration_text = " ".join((e.get("narration") or "") for e in evidence if e.get("type") == "bank_transaction").upper()

        if stage == "settlement":
            for e in evidence:
                if e.get("type") == "refund":
                    if delta == e["amount_paisa"]:
                        return json.dumps({"root_cause": "missing_refund_netting", "supporting_evidence": [e["id"]],
                                            "confidence": 0.85, "explanation": f"delta matches refund {e['id']} exactly, unnetted"})
                    if delta == -e["amount_paisa"]:
                        return json.dumps({"root_cause": "duplicate_refund", "supporting_evidence": [e["id"]],
                                            "confidence": 0.85, "explanation": f"delta matches refund {e['id']} exactly, netted twice"})
            if 0 < abs(delta) <= 5:
                return json.dumps({"root_cause": "currency_rounding", "supporting_evidence": [],
                                    "confidence": 0.80, "explanation": "delta is within a rounding-sized band"})

        bank_ids = [e["id"] for e in evidence if e.get("type") == "bank_transaction"]
        if "PROC CHG" in narration_text or "ADDL" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids,
                                "confidence": 0.85, "explanation": "bank narration references an additional charge"})
        if "BANK CHARGES" in narration_text or "NET OF" in narration_text:
            return json.dumps({"root_cause": "unmatched_external_deduction", "supporting_evidence": bank_ids,
                                "confidence": 0.85, "explanation": "bank narration references bank-side charges"})
        if "(DUP)" in narration_text:
            return json.dumps({"root_cause": "duplicate_bank_credit", "supporting_evidence": bank_ids,
                                "confidence": 0.90, "explanation": "more than one bank transaction references this settlement"})
        if "ADJ" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids,
                                "confidence": 0.45, "explanation": "vague adjustment narration, insufficient to be confident"})
        return json.dumps({"root_cause": "unknown", "supporting_evidence": [], "confidence": 0.20,
                            "explanation": "no supporting evidence found"})


# --- experiment core (mirrors run_rootcause_validation.py) ------------------------


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
    b_source: str


def run_experiment(batch: GeneratedBatch, gt_rows: list[GroundTruth], dataset_label: str, client: RootCauseLLMClient) -> list[CaseRecord]:
    gt_by_order = {g.record_id: g for g in gt_rows}
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)

    records: list[CaseRecord] = []
    for order in batch.orders:
        inputs = assemble_case_inputs(batch, result, order.order_id)
        if inputs.settlement is None or inputs.settlement_match is None:
            continue
        trace = trace_chain(
            inputs.order, inputs.payments, inputs.refunds, inputs.settlement, inputs.bank_txns,
            settlement_group_payments=inputs.settlement_group_payments or inputs.payments,
            settlement_group_refunds=inputs.settlement_group_refunds or inputs.refunds,
        )
        if trace.status != "diverged":
            continue

        gt = gt_by_order.get(order.order_id)
        true_cause = gt.true_root_cause if gt else None
        is_ambiguous = bool(gt and gt.is_ambiguous)
        group_refunds = inputs.settlement_group_refunds or inputs.refunds

        known_ids = {order.order_id}
        known_ids.update(p.payment_id for p in inputs.settlement_group_payments)
        known_ids.update(r.refund_id for r in group_refunds)
        known_ids.update(b.bank_txn_id for b in inputs.bank_txns)
        known_ids.add(inputs.settlement.settlement_id)

        # Arm A: deterministic rules only (no AI attempted at all).
        a_proposal = detect_known_cause(trace.first_divergence, group_refunds, inputs.bank_txns)
        a_resolved, a_cause = False, None
        if a_proposal is not None:
            a_verify = verify_root_cause_proposal(a_proposal, trace.first_divergence.expected_paisa, trace.first_divergence.actual_paisa, known_ids)
            if a_verify.passed:
                a_resolved, a_cause = True, a_proposal.root_cause

        # Arm B: the REAL production path (deterministic-first, AI fallback).
        case_result = investigate_case(client, trace.first_divergence, group_refunds, inputs.bank_txns)
        b_resolved, b_cause = False, None
        if case_result.proposal is not None:
            b_verify = verify_root_cause_proposal(case_result.proposal, trace.first_divergence.expected_paisa, trace.first_divergence.actual_paisa, known_ids)
            if b_verify.passed:
                b_resolved, b_cause = True, case_result.proposal.root_cause

        records.append(CaseRecord(
            order_id=order.order_id, dataset=dataset_label, true_cause=true_cause, is_ambiguous=is_ambiguous,
            a_resolved=a_resolved, a_correct=bool(a_resolved and a_cause == true_cause),
            b_resolved=b_resolved, b_correct=bool(b_resolved and b_cause == true_cause), b_source=case_result.source,
        ))

    return records


def exact_binomial_two_sided_p(k: int, n: int) -> float:
    if n == 0:
        return 1.0

    def pmf(i: int) -> float:
        return comb(n, i) * (0.5 ** n)

    pk = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= pk + 1e-12))


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
        print(f"  coverage (resolved):        {resolved}/{n} = {resolved / n:.1%}")
        print(f"  escalation rate:             {escalated}/{n} = {escalated / n:.1%}")
        print(f"  correct root-cause id rate:  {correct}/{n} = {correct / n:.1%}")
        print(f"  false resolutions:           {false_res}/{n} = {false_res / n:.1%}")
        print(f"  precision (of resolved):     {correct}/{resolved if resolved else 1} = {correct / resolved if resolved else 0:.1%}")
        print()

    summarize("A (deterministic rules only)", "a_resolved", "a_correct")
    summarize("B (production path: deterministic-first + real AI fallback)", "b_resolved", "b_correct")

    print("Arm B outcomes by source:")
    by_source: Counter = Counter(r.b_source for r in records)
    for source, count in by_source.most_common():
        correct = sum(1 for r in records if r.b_source == source and r.b_correct)
        print(f"  {source:15s} n={count:4d}  correct={correct}")
    print()

    both_correct = sum(1 for r in records if r.a_correct and r.b_correct)
    b_only_correct = sum(1 for r in records if r.b_correct and not r.a_correct)
    a_only_correct = sum(1 for r in records if r.a_correct and not r.b_correct)
    a_false = sum(1 for r in records if r.a_resolved and not r.a_correct)
    b_false = sum(1 for r in records if r.b_resolved and not r.b_correct)

    print("B vs A (paired, per case):")
    print(f"  both correct:        {both_correct}")
    print(f"  B correct, A wrong:  {b_only_correct}")
    print(f"  A correct, B wrong:  {a_only_correct}")
    print(f"  A false resolutions: {a_false}")
    print(f"  B false resolutions: {b_false}")
    print()

    discordant = b_only_correct + a_only_correct
    if discordant > 0:
        p = exact_binomial_two_sided_p(b_only_correct, discordant)
        print(f"Sign test on discordant pairs (B-favoring vs A-favoring): {b_only_correct}/{discordant} favor B, exact two-sided p={p:.4f}")
    else:
        print("No discordant pairs between A and B - cannot run a sign test (n=0).")
    print()

    print("Notable failures (B false resolutions):")
    for r in records:
        if r.b_resolved and not r.b_correct:
            print(f"  {r.dataset}/{r.order_id}: true={r.true_cause}  B resolved via {r.b_source} as wrong cause")
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
    parser.add_argument("--pool", action="store_true")
    args = parser.parse_args()

    if settings.gemini_api_key:
        client: RootCauseLLMClient = GeminiRootCauseClient(settings.gemini_api_key, settings.gemini_model)
        mode = f"REAL Gemini API ({settings.gemini_model})"
    elif settings.anthropic_api_key:
        client = AnthropicRootCauseClient(settings.anthropic_api_key, settings.anthropic_model)
        mode = f"REAL Anthropic API ({settings.anthropic_model})"
    else:
        client = _ReferenceStandInClient()
        mode = "SIMULATED - no GEMINI_API_KEY/ANTHROPIC_API_KEY set, using a rule-based stand-in, NOT a real LLM call"

    print("=== Milestone 8 root-cause evaluation ===")
    print(f"mode: {mode}")
    print()

    all_records: list[CaseRecord] = []

    if args.pool:
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
            all_records.extend(run_experiment(batch, gt_rows, dv, client))
        for seed, count in ((42, 500), (7, 400), (99, 400)):
            label = f"gen-seed{seed}-n{count}"
            batch = generate_dataset(seed=seed, num_flows=count, dataset_version=label)
            print(f"dataset: {label}  (orders={len(batch.orders)})")
            all_records.extend(run_experiment(batch, batch.ground_truth, label, client))
    elif args.dataset_version:
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
        all_records = run_experiment(batch, gt_rows, args.dataset_version, client)
    else:
        label = f"gen-seed{args.seed}-n{args.count}"
        batch = generate_dataset(seed=args.seed, num_flows=args.count, dataset_version=label)
        all_records = run_experiment(batch, batch.ground_truth, label, client)

    print()
    report(all_records)


if __name__ == "__main__":
    main()
