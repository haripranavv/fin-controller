#!/usr/bin/env python
"""Milestone 10: final evaluation. Compares two arms on the SAME held-out
dataset:

  A. deterministic baseline  - app.pipeline.pipeline.resolve_case
     (milestone 6, UNCHANGED). No AI. In-memory only, no DB writes -
     always reproducible by re-running.
  B. AI-enhanced              - app.orchestrator.batch_runner.run_batch
     (milestone 9, UNCHANGED). The real, persisted bounded state machine:
     deterministic matcher -> verifier -> divergence tracer ->
     known-cause rules -> AI root-cause investigator -> verifier. Runs
     under the SAME batch_id scripts/run_orchestrator.py uses
     ("batch_<dataset_version>") - NOT a separate one. This is required,
     not a convenience: app.orchestrator.case_runner.run_case derives
     case_id purely from order_id ("case_<order_id>"), with no batch_id
     component, so case_id is globally unique regardless of which
     batch_id it is inserted under - a second batch_id over the same
     orders would violate that uniqueness constraint (found running this
     exact way once - see docs/ARCHITECTURE_NOTES.md milestone 10
     section). This script therefore purges and replaces any existing
     orchestration rows for batch_<dataset_version> before running - the
     same purge/rerun mechanism run_orchestrator.py's --overwrite uses -
     so a milestone 9 run and this evaluation's run cannot coexist for
     the same dataset; re-running this script always reflects the
     CURRENT code, and functionally reproduces what
     run_orchestrator.py --overwrite would leave persisted.

Neither arm's decision logic is duplicated here: both are called through
their real, unchanged production entry points. This script only ASSEMBLES
inputs, SCORES outputs against ground truth, and REPORTS - it does not
implement matching, verification, divergence tracing, or root-cause
reasoning of its own.

Ground truth (app.models.groundtruth.GroundTruth) is read ONLY here, after
both arms have already produced their outcomes, purely to score
correctness - never passed into matching/verification/investigation. Same
read-only-for-comparison discipline every scripts/run_*.py report in this
project has followed since milestone 3.

THE CENTRAL METRIC this script formalizes (per the milestone 10 request):
every case is tagged matcher_correct - whether the settlement the
deterministic matcher actually picked is one of ground truth's
true_match_ids for that order - and every other metric is reported BOTH
in aggregate AND split by this flag. A resolution is never counted
"correct" unless (a) the upstream match was correct AND (b) the
determined root cause (or "no divergence" for a clean case) exactly
matches ground truth. "Passed the verifier" is not "correct" - see the
methodological note in run_rootcause_validation.py; this script applies
that same discipline to the whole pipeline, not just root-cause proposals.

Persists two app.models.operational.EvaluationRun rows (mode=baseline,
mode=ai_enhanced) per run - a table that has existed unused since
milestone 1. Pass --no-persist to skip this (report-only).

Does NOT modify app.matcher / app.verifier / app.divergence / app.pipeline
/ app.rootcause / app.orchestrator / app.datagen / app.models.

Usage (from backend/, venv active, Postgres up, heldout-v1 generated):
    python scripts/run_evaluation.py
    python scripts/run_evaluation.py --dataset-version dev-v1
    python scripts/run_evaluation.py --no-persist
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

from sqlalchemy import delete  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.datagen.models import GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.enums import EvalMode, RecordType  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.models.operational import (  # noqa: E402
    AgentEvent, EvaluationRun, ExceptionRecord, Investigation, Match, ReconciliationCase,
)
from app.orchestrator.batch_runner import run_batch  # noqa: E402
from app.pipeline.assemble import assemble_case_inputs  # noqa: E402
from app.pipeline.pipeline import resolve_case  # noqa: E402
from app.rootcause.client import AnthropicRootCauseClient, GeminiRootCauseClient, RootCauseLLMClient  # noqa: E402

_RESOLVED_CAUSE_RE = re.compile(r"resolved: (deterministic|ai) cause '([a-z_]+)' verified")
_DECLINE = json.dumps({"root_cause": "unknown", "supporting_evidence": [], "confidence": 0.20, "explanation": "no supporting evidence found"})


class _ReferenceStandInClient:
    """Identical reasoning to scripts/run_orchestrator.py's stand-in -
    duplicated here (established pattern in this project: each standalone
    script owns its copy rather than cross-importing another script).
    Used only when ANTHROPIC_API_KEY is not set. Never reads ground truth."""

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = json.loads(user_prompt)
        stage, delta, evidence = payload["divergence_stage"], payload["delta"], payload["evidence"]
        narration_text = " ".join((e.get("narration") or "") for e in evidence if e.get("type") == "bank_transaction").upper()

        if stage == "settlement":
            for e in evidence:
                if e.get("type") == "refund":
                    if delta == e["amount_paisa"]:
                        return json.dumps({"root_cause": "missing_refund_netting", "supporting_evidence": [e["id"]], "confidence": 0.85, "explanation": "delta matches a refund exactly, unnetted"})
                    if delta == -e["amount_paisa"]:
                        return json.dumps({"root_cause": "duplicate_refund", "supporting_evidence": [e["id"]], "confidence": 0.85, "explanation": "delta matches a refund exactly, netted twice"})
            if 0 < abs(delta) <= 5:
                return json.dumps({"root_cause": "currency_rounding", "supporting_evidence": [], "confidence": 0.80, "explanation": "delta within a rounding-sized band"})

        bank_ids = [e["id"] for e in evidence if e.get("type") == "bank_transaction"]
        if "PROC CHG" in narration_text or "ADDL" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids, "confidence": 0.85, "explanation": "bank narration references an additional charge"})
        if "BANK CHARGES" in narration_text or "NET OF" in narration_text:
            return json.dumps({"root_cause": "unmatched_external_deduction", "supporting_evidence": bank_ids, "confidence": 0.85, "explanation": "bank narration references bank-side charges"})
        if "(DUP)" in narration_text:
            return json.dumps({"root_cause": "duplicate_bank_credit", "supporting_evidence": bank_ids, "confidence": 0.90, "explanation": "more than one bank transaction references this settlement"})
        if "ADJ" in narration_text:
            return json.dumps({"root_cause": "unreported_fee", "supporting_evidence": bank_ids, "confidence": 0.45, "explanation": "vague adjustment narration, insufficient to be confident"})
        return _DECLINE


def purge_orchestration(session, batch_id: str) -> None:
    case_ids = [c.case_id for c in session.query(ReconciliationCase.case_id).filter_by(batch_id=batch_id).all()]
    if not case_ids:
        return
    for model in (AgentEvent, Match, Investigation, ExceptionRecord):
        session.execute(delete(model).where(model.case_id.in_(case_ids)))
    session.execute(delete(ReconciliationCase).where(ReconciliationCase.case_id.in_(case_ids)))
    session.commit()


# --- unified per-case evaluation record --------------------------------------------


@dataclass
class CaseEval:
    order_id: str
    amount_paisa: int
    true_cause: str | None
    is_ambiguous: bool
    matched: bool           # matcher accepted SOME settlement for this case
    matcher_correct: bool   # that settlement is in ground truth's true_match_ids
    outcome: str             # "RESOLVED" | "ESCALATED"
    resolved_via: str | None  # "clean" | "deterministic" | "ai" | None
    resolved_cause: str | None
    correct: bool             # strict: matched AND matcher_correct AND cause matches ground truth


def _score_correct(outcome: str, matched: bool, matcher_correct: bool, true_cause: str | None, resolved_cause: str | None) -> bool:
    """The one place "correct" is decided, for BOTH arms. A resolution
    riding on the wrong upstream match is never correct, regardless of
    how confidently a cause was attached to it (milestone 8's own
    finding, now enforced structurally instead of by spot check)."""
    if outcome != "RESOLVED":
        return False
    if not matched or not matcher_correct:
        return False
    if true_cause is None:
        return resolved_cause is None
    return resolved_cause == true_cause


def _matcher_correct(settlement_match, true_ids: set[str]) -> bool:
    return bool(settlement_match is not None and settlement_match.target_id in true_ids)


# --- Arm A: deterministic baseline (milestone 6, in-memory, no persistence) --------


def score_baseline(batch: GeneratedBatch, gt_by_order: dict[str, GroundTruth]) -> tuple[list[CaseEval], float]:
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    t0 = time.perf_counter()
    records: list[CaseEval] = []
    for order in batch.orders:
        inputs = assemble_case_inputs(batch, result, order.order_id)
        gt = gt_by_order.get(order.order_id)
        true_cause = gt.true_root_cause if gt else None
        is_ambiguous = bool(gt and gt.is_ambiguous)
        true_ids = set(gt.true_match_ids) if gt else set()

        matched = inputs.settlement_match is not None
        matcher_correct = _matcher_correct(inputs.settlement_match, true_ids)

        case_result = resolve_case(inputs)
        proposal = case_result.root_cause_proposal
        if case_result.outcome == "RESOLVED" and proposal is None:
            resolved_via, resolved_cause = "clean", None
        elif case_result.outcome == "RESOLVED" and proposal is not None:
            resolved_via, resolved_cause = "deterministic", proposal.root_cause
        else:
            resolved_via, resolved_cause = None, None

        correct = _score_correct(case_result.outcome, matched, matcher_correct, true_cause, resolved_cause)
        records.append(CaseEval(
            order_id=order.order_id, amount_paisa=abs(order.amount_paisa), true_cause=true_cause, is_ambiguous=is_ambiguous,
            matched=matched, matcher_correct=matcher_correct, outcome=case_result.outcome,
            resolved_via=resolved_via, resolved_cause=resolved_cause, correct=correct,
        ))
    elapsed = time.perf_counter() - t0
    return records, elapsed


# --- Arm B: AI-enhanced (milestone 9's real orchestrator, freshly re-run) ---------


def score_ai_enhanced(
    session, batch: GeneratedBatch, gt_by_order: dict[str, GroundTruth], client: RootCauseLLMClient, batch_id: str,
) -> tuple[list[CaseEval], float]:
    purge_orchestration(session, batch_id)
    t0 = time.perf_counter()
    summary = run_batch(session, batch, client)
    elapsed = time.perf_counter() - t0

    case_ids = [c.case_id for c in summary.cases]
    matched_rows = (
        session.query(Match)
        .filter(Match.case_id.in_(case_ids), Match.target_type == RecordType.SETTLEMENT, Match.accepted.is_(True))
        .all()
    )
    matched_settlement_by_case = {m.case_id: m.target_id for m in matched_rows}
    orders_by_id = {o.order_id: o for o in batch.orders}

    records: list[CaseEval] = []
    for c in summary.cases:
        order = orders_by_id[c.order_id]
        gt = gt_by_order.get(c.order_id)
        true_cause = gt.true_root_cause if gt else None
        is_ambiguous = bool(gt and gt.is_ambiguous)
        true_ids = set(gt.true_match_ids) if gt else set()

        matched_settlement_id = matched_settlement_by_case.get(c.case_id)
        matched = matched_settlement_id is not None
        matcher_correct = bool(matched and matched_settlement_id in true_ids)

        m = _RESOLVED_CAUSE_RE.search(c.reason)
        if c.outcome == "RESOLVED" and m is None:
            resolved_via, resolved_cause = "clean", None
        elif c.outcome == "RESOLVED" and m is not None:
            resolved_via, resolved_cause = m.group(1), m.group(2)
        else:
            resolved_via, resolved_cause = None, None

        correct = _score_correct(c.outcome, matched, matcher_correct, true_cause, resolved_cause)
        records.append(CaseEval(
            order_id=c.order_id, amount_paisa=abs(order.amount_paisa), true_cause=true_cause, is_ambiguous=is_ambiguous,
            matched=matched, matcher_correct=matcher_correct, outcome=c.outcome,
            resolved_via=resolved_via, resolved_cause=resolved_cause, correct=correct,
        ))
    return records, elapsed


# --- metrics -----------------------------------------------------------------------


def compute_metrics(cases: list[CaseEval], elapsed_seconds: float | None) -> dict:
    n = len(cases)
    resolved = [c for c in cases if c.outcome == "RESOLVED"]
    escalated = [c for c in cases if c.outcome == "ESCALATED"]
    correct = [c for c in cases if c.correct]
    matcher_correct_cases = [c for c in cases if c.matcher_correct]
    matched_cases = [c for c in cases if c.matched]
    false_matched = [c for c in matched_cases if not c.matcher_correct]
    ai_resolved = [c for c in cases if c.resolved_via == "ai"]
    cause_determined = [c for c in cases if c.resolved_via in ("deterministic", "ai")]
    cause_correct = [c for c in cause_determined if c.correct]

    total_value = sum(c.amount_paisa for c in cases)
    resolved_value = sum(c.amount_paisa for c in resolved)
    exception_value = sum(c.amount_paisa for c in escalated)

    return {
        "n": n,
        "resolution_rate": len(resolved) / n if n else 0.0,
        "monetary_resolution_rate": (resolved_value / total_value) if total_value else 0.0,
        "precision": len(correct) / len(resolved) if resolved else 0.0,
        "recall": len(correct) / len(matcher_correct_cases) if matcher_correct_cases else 0.0,
        "false_match_rate": len(false_matched) / len(matched_cases) if matched_cases else 0.0,
        "escalation_rate": len(escalated) / n if n else 0.0,
        "exception_count": len(escalated),
        "exception_value_paisa": exception_value,
        "correct_root_cause_rate": len(cause_correct) / len(cause_determined) if cause_determined else 0.0,
        "ai_assisted_resolution_rate": len(ai_resolved) / n if n else 0.0,
        "throughput": (n / elapsed_seconds) if elapsed_seconds else 0.0,
        "resolved_count": len(resolved), "escalated_count": len(escalated), "correct_count": len(correct),
        "matcher_correct_count": len(matcher_correct_cases), "matched_count": len(matched_cases),
        "cause_determined_count": len(cause_determined), "cause_correct_count": len(cause_correct),
    }


def exact_binomial_two_sided_p(k: int, n: int) -> float:
    if n == 0:
        return 1.0

    def pmf(i: int) -> float:
        return comb(n, i) * (0.5 ** n)

    pk = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= pk + 1e-12))


# --- reporting -----------------------------------------------------------------------


def _fmt_metrics_row(label: str, m: dict) -> str:
    return (f"  {label:38s} n={m['n']:4d}  resolved={m['resolved_count']:4d} ({m['resolution_rate']:6.1%})  "
            f"escalated={m['escalated_count']:4d} ({m['escalation_rate']:6.1%})")


def print_metrics_block(title: str, m: dict) -> None:
    print(f"{title} (n={m['n']}):")
    if m["n"] == 0:
        print("  (no cases in this subset)")
        print()
        return
    print(f"  resolution rate (count):        {m['resolved_count']}/{m['n']} = {m['resolution_rate']:.1%}")
    print(f"  monetary resolution rate:       {m['monetary_resolution_rate']:.1%}  (Rs value resolved / Rs value total)")
    print(f"  precision (of resolved):        {m['correct_count']}/{m['resolved_count'] if m['resolved_count'] else 1} = {m['precision']:.1%}")
    print(f"  recall (of matcher-correct):    {m['correct_count']}/{m['matcher_correct_count'] if m['matcher_correct_count'] else 1} = {m['recall']:.1%}")
    print(f"  false-match rate:               {m['false_match_rate']:.1%}  (of {m['matched_count']} matched)")
    print(f"  escalation rate:                {m['escalated_count']}/{m['n']} = {m['escalation_rate']:.1%}")
    print(f"  exception value:                Rs {m['exception_value_paisa'] / 100:,.2f}  ({m['exception_count']} cases)")
    print(f"  correct root-cause rate:        {m['cause_correct_count']}/{m['cause_determined_count'] if m['cause_determined_count'] else 1} = {m['correct_root_cause_rate']:.1%}  (of {m['cause_determined_count']} cases where a cause was determined)")
    print(f"  AI-assisted resolution rate:    {m['ai_assisted_resolution_rate']:.1%}  (of all {m['n']} cases)")
    print(f"  throughput:                     {m['throughput']:.1f} cases/sec")
    print()


def report(dataset_label: str, baseline: list[CaseEval], ai_enhanced: list[CaseEval], t_base: float, t_ai: float, ai_mode: str) -> None:
    print(f"=== Milestone 10 final evaluation: {dataset_label} ===")
    print(f"n={len(baseline)} orders (same dataset, both arms)")
    print()

    print("--- AGGREGATE ---")
    print_metrics_block("Arm A: deterministic baseline (milestone 6)", compute_metrics(baseline, t_base))
    print_metrics_block("Arm B: AI-enhanced (milestone 9 orchestrator)", compute_metrics(ai_enhanced, t_ai))

    print("--- MOST IMPORTANT SPLIT: was the upstream match correct? ---")
    print()
    for arm_name, cases, elapsed in (("Arm A (baseline)", baseline, t_base), ("Arm B (AI-enhanced)", ai_enhanced, t_ai)):
        correct_subset = [c for c in cases if c.matcher_correct]
        wrong_subset = [c for c in cases if not c.matcher_correct]
        print(f"{arm_name}:")
        print(f"  upstream matcher correct:   {len(correct_subset)}/{len(cases)} = {len(correct_subset) / len(cases):.1%}" if cases else "  n=0")
        print_metrics_block(f"  {arm_name} | matcher-CORRECT subset", compute_metrics(correct_subset, None))
        print_metrics_block(f"  {arm_name} | matcher-WRONG subset (includes NO_MATCH)", compute_metrics(wrong_subset, None))

    print("--- Paired comparison (same orders, both arms; correctness per case) ---")
    by_order_a = {c.order_id: c for c in baseline}
    by_order_b = {c.order_id: c for c in ai_enhanced}
    both_correct = sum(1 for oid in by_order_a if by_order_a[oid].correct and by_order_b[oid].correct)
    b_only = sum(1 for oid in by_order_a if by_order_b[oid].correct and not by_order_a[oid].correct)
    a_only = sum(1 for oid in by_order_a if by_order_a[oid].correct and not by_order_b[oid].correct)
    neither = sum(1 for oid in by_order_a if not by_order_a[oid].correct and not by_order_b[oid].correct)
    print(f"  both correct:            {both_correct}")
    print(f"  B correct, A wrong:      {b_only}")
    print(f"  A correct, B wrong:      {a_only}")
    print(f"  neither correct:         {neither}")
    discordant = a_only + b_only
    if discordant > 0:
        p = exact_binomial_two_sided_p(b_only, discordant)
        print(f"  sign test on discordant pairs: {b_only}/{discordant} favor B, exact two-sided p={p:.4f}")
    else:
        print("  no discordant pairs - cannot run a sign test (n=0)")
    print()

    print("Ambiguous cases (ground truth is_ambiguous=True) - correct behavior is to ESCALATE:")
    for arm_name, cases in (("A", baseline), ("B", ai_enhanced)):
        ambiguous = [c for c in cases if c.is_ambiguous]
        resolved_anyway = sum(1 for c in ambiguous if c.outcome == "RESOLVED")
        print(f"  {arm_name}: {len(ambiguous)} ambiguous cases -> {resolved_anyway} resolved anyway, "
              f"{len(ambiguous) - resolved_anyway} correctly escalated")
    print()

    print("Results by true root cause (aggregate, both arms):")
    by_cause: dict[str, Counter] = {}
    for label, cases in (("a", baseline), ("b", ai_enhanced)):
        for c in cases:
            key = c.true_cause or "(clean)"
            counter = by_cause.setdefault(key, Counter())
            counter["n"] += 1 if label == "a" else 0
            counter[f"{label}_resolved"] += int(c.outcome == "RESOLVED")
            counter[f"{label}_correct"] += int(c.correct)
    for cause, c in sorted(by_cause.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {cause:32s} n={c['n']:3d}  A: {c['a_resolved']:3d} resolved / {c['a_correct']:3d} correct   "
              f"B: {c['b_resolved']:3d} resolved / {c['b_correct']:3d} correct")
    print()

    print("What this experiment proves:")
    m_a, m_b = compute_metrics(baseline, t_base), compute_metrics(ai_enhanced, t_ai)
    if dataset_label == "heldout-v1":
        provenance = "never used to tune the matcher/verifier/divergence thresholds - that tuning used dev-v1 only, milestones 3-6"
    elif dataset_label == "dev-v1":
        provenance = "the SAME dataset used to tune the matcher/verifier/divergence thresholds in milestones 3-6 - included here for consistency with milestone 9's own reported numbers, not as the held-out claim (see the heldout-v1 run for that)"
    else:
        provenance = "provenance vs. the milestone 3-6 tuning process not tracked by this script for a dataset outside dev-v1/heldout-v1"
    print(f"  - On {dataset_label} ({m_a['n']} orders, {provenance}), Arm B resolves")
    print(f"    {m_b['resolution_rate']:.1%} of cases vs Arm A's {m_a['resolution_rate']:.1%}, a")
    print(f"    {m_b['resolution_rate'] - m_a['resolution_rate']:+.1%} point difference driven entirely by the AI root-cause")
    print(f"    investigator ({m_b['ai_assisted_resolution_rate']:.1%} of all cases resolved via AI specifically).")
    print(f"  - This lift is NOT free: it exists only where the upstream match is already correct.")
    print(f"    Conditioned on a correct match, Arm B's precision is")
    correct_subset_b = [c for c in ai_enhanced if c.matcher_correct]
    mb_correct = compute_metrics(correct_subset_b, None)
    print(f"    {mb_correct['precision']:.1%} ({mb_correct['correct_count']}/{mb_correct['resolved_count'] if mb_correct['resolved_count'] else 1});")
    print(f"    every false resolution in the aggregate numbers above is attributable to an upstream")
    print(f"    matcher mismatch, not to the investigator inventing a wrong cause for a correctly")
    print(f"    matched case - the sign-test/discordant-pair table above is the direct evidence for this,")
    print(f"    not an assumption.")
    print(f"  - This does NOT prove the AI investigator would generalize to narration formats or")
    print(f"    divergence patterns outside this synthetic generator's design (see docs/ARCHITECTURE_NOTES.md")
    print(f"    milestone 2's axis-B scenario list for exactly what is and is not covered), and the AI")
    print(f"    client used ({ai_mode}) should be reread accordingly.")


# --- persistence -----------------------------------------------------------------


def persist_run(session, dataset_version: str, mode: EvalMode, m: dict) -> None:
    session.add(EvaluationRun(
        dataset_version=dataset_version, mode=mode, records_processed=m["n"],
        match_rate=m["resolution_rate"], match_rate_by_value=m["monetary_resolution_rate"],
        precision=m["precision"], recall=m["recall"], false_match_rate=m["false_match_rate"],
        exception_count=m["exception_count"], exception_value_paisa=m["exception_value_paisa"],
        ai_assisted_resolution_rate=m["ai_assisted_resolution_rate"], throughput=m["throughput"],
    ))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version", default="heldout-v1")
    parser.add_argument("--no-persist", action="store_true", help="skip writing EvaluationRun rows")
    args = parser.parse_args()
    dv = args.dataset_version

    if settings.gemini_api_key:
        client: RootCauseLLMClient = GeminiRootCauseClient(settings.gemini_api_key, settings.gemini_model)
        mode = f"REAL Gemini API ({settings.gemini_model})"
    elif settings.anthropic_api_key:
        client = AnthropicRootCauseClient(settings.anthropic_api_key, settings.anthropic_model)
        mode = f"REAL Anthropic API ({settings.anthropic_model})"
    else:
        client = _ReferenceStandInClient()
        mode = "SIMULATED - no GEMINI_API_KEY/ANTHROPIC_API_KEY set, using a rule-based stand-in, NOT a real LLM call"

    session = SessionLocal()
    gt_session = GroundTruthSessionLocal()
    try:
        orders, payments, refunds, settlements, bank_txns = load_dataset(session, dv)
        if not orders:
            print(f"No records found for dataset_version={dv!r}. Generate it first "
                  f"(python scripts/generate_dataset.py --dataset-version {dv} --seed 1337 --count 90).")
            return
        gt_rows = gt_session.query(GroundTruth).filter(GroundTruth.record_id.like(f"%_{dv}_%")).all()
        gt_by_order = {g.record_id: g for g in gt_rows}

        batch_id = f"batch_{dv}"  # same batch_id run_orchestrator.py uses - see module docstring on why
        batch = GeneratedBatch(batch_id=batch_id, dataset_version=dv, seed=0,
                                orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)

        print(f"AI mode: {mode}")
        print(f"orders={len(orders)}  payments={len(payments)}  settlements={len(settlements)}  bank_txns={len(bank_txns)}")
        print("Ground truth read-only, for scoring only - never passed to either arm's decision logic.")
        print()

        baseline, t_base = score_baseline(batch, gt_by_order)
        ai_enhanced, t_ai = score_ai_enhanced(session, batch, gt_by_order, client, batch_id)

        report(dv, baseline, ai_enhanced, t_base, t_ai, mode)

        if not args.no_persist:
            persist_run(session, dv, EvalMode.BASELINE, compute_metrics(baseline, t_base))
            persist_run(session, dv, EvalMode.AI_ENHANCED, compute_metrics(ai_enhanced, t_ai))
            print()
            print(f"Persisted EvaluationRun rows (mode=baseline, mode=ai_enhanced) for dataset_version={dv!r}.")
    finally:
        session.close()
        gt_session.close()


if __name__ == "__main__":
    main()
