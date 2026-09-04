#!/usr/bin/env python
"""Real Gemini smoke test — exercises the ACTUAL, unchanged production
path end to end on real persisted cases:

    app.rootcause.case.investigate_case's own precedence
      -> (only when detect_known_cause returns None, i.e. genuinely
          needs AI) app.rootcause.investigator.investigate_root_cause
      -> GeminiRootCauseClient.complete_json (REAL network call to Gemini)
      -> RootCauseInvestigation Pydantic schema validation (extra="forbid")
      -> the 0.60 confidence gate
      -> app.rootcause.investigator.to_root_cause_proposal
      -> app.verifier.checks.verify_root_cause_proposal (REAL verifier)
      -> RESOLVED / ESCALATED, exactly as app.orchestrator.case_runner
         .run_case would decide it

Gemini never resolves anything by itself here or in production - every
case still ends at the same unchanged confidence gate and verifier
app.orchestrator.case_runner.run_case uses; this script only observes
that path, it does not shortcut it.

Does not persist anything to Postgres (no ReconciliationCase/AgentEvent/
Match/Investigation rows written) — this is a read-and-report diagnostic,
not a batch run. Selects real, already-persisted cases whose deterministic
matcher already accepted a settlement and whose divergence trace already
diverged with NO deterministic known cause (the exact population
app.rootcause.case.investigate_case would hand to the AI for real) — never
synthesizes a case, never reads ground truth to pick or judge them (only
used, read-only, at the very end to label which true root cause each case
actually is, for readability — never fed to the AI or the verifier).

Deliberately targets a SMALL, capped number of real generation calls
(--max-real-calls, default 9) spread across narration-signal buckets
(clear-hint / vague-hint / sparse-evidence) to get a representative,
non-random sample instead of burning quota on whatever the id ordering
happens to produce. Also runs exactly ONE real call with a deliberately
invalid API key (zero generation-quota cost - it fails at auth, before
any inference) to exercise the "API/error -> safe escalation" path for
real without spending a real generation call on it.

Captures, per real call, via a non-invasive spy on the SDK client object
(no change to app/rootcause/client.py): finish_reason, token usage, and
wall-clock latency — on top of what the production return value already
carries (schema validity, confidence, verifier result, final outcome).

Requires a real GEMINI_API_KEY in backend/.env.

Usage:
    python scripts/gemini_smoke_test.py
    python scripts/gemini_smoke_test.py --max-real-calls 9 --dataset-version heldout-v1
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.datagen.models import GeneratedBatch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.divergence.tracer import trace_chain  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.pipeline.assemble import assemble_case_inputs  # noqa: E402
from app.pipeline.known_causes import detect_known_cause  # noqa: E402
from app.rootcause.client import GeminiRootCauseClient  # noqa: E402
from app.rootcause.evidence import build_evidence  # noqa: E402
from app.rootcause.investigator import MIN_CONFIDENCE, investigate_root_cause, to_root_cause_proposal  # noqa: E402
from app.verifier.checks import verify_root_cause_proposal  # noqa: E402

_CLEAR_HINTS = ("PROC CHG", "ADDL", "NET OF", "BANK CHARGES", "(DUP)")
_VAGUE_HINTS = ("ADJ",)


class InstrumentedGeminiClient:
    """Wraps a real, UNCHANGED GeminiRootCauseClient and spies on the one
    underlying SDK call it makes, to record finish_reason/usage/latency
    for reporting — without altering the request, the response, or any
    decision logic. complete_json() below still runs the real, unmodified
    production method end to end; the spy only observes it."""

    def __init__(self, api_key: str, model: str) -> None:
        self._inner = GeminiRootCauseClient(api_key, model)
        self.last_finish_reason: str | None = None
        self.last_usage: object | None = None
        self.last_latency_ms: float | None = None
        self.last_call_succeeded: bool | None = None

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        captured: dict = {}
        original = self._inner._client.models.generate_content

        def spy(*args, **kwargs):
            t0 = time.perf_counter()
            resp = original(*args, **kwargs)
            captured["latency_ms"] = (time.perf_counter() - t0) * 1000
            captured["response"] = resp
            return resp

        self.last_finish_reason = None
        self.last_usage = None
        self.last_latency_ms = None
        self.last_call_succeeded = None
        try:
            with patch.object(self._inner._client.models, "generate_content", side_effect=spy):
                text = self._inner.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
            self.last_call_succeeded = True
        finally:
            if "response" in captured:
                resp = captured["response"]
                if resp.candidates:
                    fr = resp.candidates[0].finish_reason
                    self.last_finish_reason = getattr(fr, "name", str(fr))
                self.last_usage = resp.usage_metadata
                self.last_latency_ms = captured["latency_ms"]
            if self.last_call_succeeded is None:
                self.last_call_succeeded = False
        return text


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


def _bucket_of(evidence: list[dict]) -> str:
    narration = " ".join((e.get("narration") or "") for e in evidence if e.get("type") == "bank_transaction").upper()
    if any(h in narration for h in _CLEAR_HINTS):
        return "clear_hint"
    if any(h in narration for h in _VAGUE_HINTS):
        return "vague_hint"
    if not evidence:
        return "no_evidence"
    return "sparse_or_other"


def collect_ai_population(batch: GeneratedBatch) -> list[dict]:
    """Every real order whose matched settlement diverged with no known
    deterministic cause - the exact precondition investigate_case uses to
    decide the AI must be called."""
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    candidates = []
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
        group_refunds = inputs.settlement_group_refunds or inputs.refunds
        if detect_known_cause(trace.first_divergence, group_refunds, inputs.bank_txns) is not None:
            continue  # deterministic rules already cover this - AI would never be called for real
        evidence = build_evidence(group_refunds, inputs.bank_txns)
        candidates.append({"order": order, "inputs": inputs, "trace": trace, "group_refunds": group_refunds,
                            "evidence": evidence, "bucket": _bucket_of(evidence)})

    gt_session = GroundTruthSessionLocal()
    try:
        gt_rows = gt_session.query(GroundTruth).filter(GroundTruth.record_id.like(f"%_{batch.dataset_version}_%")).all()
    finally:
        gt_session.close()
    gt_by_order = {g.record_id: g for g in gt_rows}
    for c in candidates:
        gt = gt_by_order.get(c["order"].order_id)
        c["true_cause"] = gt.true_root_cause if gt else None
        c["is_ambiguous"] = bool(gt and gt.is_ambiguous)
    return candidates


def select_targeted_cases(candidates: list[dict], budget: int) -> list[dict]:
    """A deliberate, capped, representative sample instead of a random or
    id-ordered slice: prioritizes ground-truth-ambiguous cases and each
    narration-signal bucket (clear/vague/no-evidence/other) in turn, so a
    small real-call budget still covers distinct real behavior."""
    ambiguous = [c for c in candidates if c["is_ambiguous"]]
    buckets = {"clear_hint": [], "vague_hint": [], "no_evidence": [], "sparse_or_other": []}
    for c in candidates:
        if c not in ambiguous:
            buckets[c["bucket"]].append(c)

    selected: list[dict] = []
    if ambiguous:
        selected.append(ambiguous[0])
    order = ["clear_hint", "clear_hint", "vague_hint", "vague_hint", "no_evidence", "sparse_or_other", "sparse_or_other"]
    for key in order:
        if len(selected) >= budget:
            break
        pool = buckets[key]
        if pool:
            selected.append(pool.pop(0))
    # top up from whatever remains if the budget isn't filled yet
    remaining = [c for c in candidates if c not in selected]
    i = 0
    while len(selected) < budget and i < len(remaining):
        selected.append(remaining[i])
        i += 1
    return selected[:budget]


def run_auth_error_case(model: str) -> None:
    """Real network call, deliberately invalid API key - fails at auth
    before any inference, so it costs no generation quota. Exercises the
    real fail-safe path (investigate_root_cause's try/except -> ESCALATED)
    against a genuine API error, not a simulated one."""
    print("--- Case 0/N: deliberate auth failure (zero generation-quota cost) ---")
    print("  purpose: exercise the real API/transport-error -> safe-escalation path for real")
    bad_client = GeminiRootCauseClient("invalid-test-key-deliberately-wrong", model)
    t0 = time.perf_counter()
    outcome = investigate_root_cause(bad_client, "settlement", 100000, 90000, -10000,
                                      [{"id": "bnk_test", "type": "bank_transaction", "amount_paisa": 90000, "narration": "TEST"}])
    latency_ms = (time.perf_counter() - t0) * 1000
    print(f"  model success/failure: {'FAILURE (expected)' if outcome.error else 'SUCCESS (unexpected!)'}")
    print(f"  latency: {latency_ms:.0f}ms")
    print(f"  error detail: {outcome.error}")
    final = "RESOLVED" if (outcome.investigation and outcome.passed_confidence_gate) else "ESCALATED"
    print(f"  -> OUTCOME: {final} ({'correct fail-safe behavior' if final == 'ESCALATED' else 'UNEXPECTED - investigate'})")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version", default="heldout-v1")
    parser.add_argument("--max-real-calls", type=int, default=9,
                         help="hard cap on real Gemini generation calls (excludes the free auth-error test)")
    parser.add_argument("--skip-auth-error-test", action="store_true")
    args = parser.parse_args()

    if not settings.gemini_api_key:
        print("No GEMINI_API_KEY configured (backend/.env). Aborting - refusing to silently fall back to a stand-in.")
        sys.exit(1)

    print(f"=== Real Gemini smoke test: {settings.gemini_model} ===")
    print(f"dataset: {args.dataset_version}   max_real_calls={args.max_real_calls}")
    print()

    if not args.skip_auth_error_test:
        run_auth_error_case(settings.gemini_model)

    session = SessionLocal()
    try:
        orders, payments, refunds, settlements, bank_txns = load_dataset(session, args.dataset_version)
    finally:
        session.close()
    batch = GeneratedBatch(batch_id=f"smoke_{args.dataset_version}", dataset_version=args.dataset_version, seed=0,
                            orders=orders, payments=payments, refunds=refunds, settlements=settlements, bank_transactions=bank_txns)

    candidates = collect_ai_population(batch)
    population = select_targeted_cases(candidates, args.max_real_calls)
    by_bucket = {}
    for c in candidates:
        by_bucket[c["bucket"]] = by_bucket.get(c["bucket"], 0) + 1
    print(f"AI-eligible population: {len(candidates)} case(s) total "
          f"(buckets: {by_bucket}, ambiguous: {sum(1 for c in candidates if c['is_ambiguous'])})")
    print(f"Selected for real calls: {len(population)} case(s) - hard cap {args.max_real_calls}\n")

    client = InstrumentedGeminiClient(settings.gemini_api_key, settings.gemini_model)

    results = []
    calls_made = 0
    for i, c in enumerate(population, 1):
        if calls_made >= args.max_real_calls:
            print(f"Hit --max-real-calls={args.max_real_calls}, stopping before case {c['order'].order_id}.")
            break
        order = c["order"]
        inputs = c["inputs"]
        trace = c["trace"]
        fd = trace.first_divergence
        evidence = c["evidence"]
        known_ids = known_ids_for(inputs)

        print(f"--- Case {i}/{len(population)}: {order.order_id}  "
              f"(bucket={c['bucket']}  ambiguous={c['is_ambiguous']}  true_cause={c['true_cause']!r}) ---")
        print(f"  divergence: stage={fd.stage}  expected={fd.expected_paisa}p  actual={fd.actual_paisa}p  delta={fd.delta_paisa}p")
        print(f"  evidence sent to Gemini: {evidence}")

        calls_made += 1
        outcome = investigate_root_cause(client, fd.stage, fd.expected_paisa, fd.actual_paisa, fd.delta_paisa, evidence)

        print(f"  model success/failure: {'SUCCESS' if client.last_call_succeeded else 'FAILURE'}")
        print(f"  finish_reason: {client.last_finish_reason}   latency: "
              f"{client.last_latency_ms:.0f}ms" if client.last_latency_ms is not None else "  latency: n/a")
        print(f"  usage: {client.last_usage}")
        print(f"  RAW GEMINI RESPONSE: {outcome.raw_response}")

        record = {
            "order_id": order.order_id, "bucket": c["bucket"], "is_ambiguous": c["is_ambiguous"],
            "true_cause": c["true_cause"], "model_success": client.last_call_succeeded,
            "finish_reason": client.last_finish_reason, "latency_ms": client.last_latency_ms,
        }

        if outcome.error is not None:
            print(f"  -> schema/transport error: {outcome.error}")
            print(f"  -> OUTCOME: ESCALATED (AI investigation failed)")
            record.update({"schema_valid": False, "final": "ESCALATED", "reason": f"error: {outcome.error}"})
            results.append(record)
            print()
            continue

        inv = outcome.investigation
        print(f"  parsed & schema-validated: root_cause={inv.root_cause.value!r}  confidence={inv.confidence:.2f}  "
              f"supporting_evidence={inv.supporting_evidence}")
        print(f"  explanation: {inv.explanation}")
        print(f"  confidence gate (>= {MIN_CONFIDENCE:.2f}): {'PASSED' if outcome.passed_confidence_gate else 'FAILED'}")
        record.update({"schema_valid": True, "proposed_cause": inv.root_cause.value, "confidence": inv.confidence})

        if not outcome.passed_confidence_gate:
            print(f"  -> OUTCOME: ESCALATED (below confidence gate, correctly not trusted)")
            record.update({"final": "ESCALATED", "reason": f"confidence {inv.confidence:.2f} < {MIN_CONFIDENCE:.2f}",
                            "verifier_ran": False})
            results.append(record)
            print()
            continue

        proposal = to_root_cause_proposal(inv, fd.delta_paisa)
        verification = verify_root_cause_proposal(proposal, fd.expected_paisa, fd.actual_paisa, known_ids)
        print(f"  VERIFIER RESULT: passed={verification.passed}")
        for check in verification.checks:
            print(f"    [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}")

        final = "RESOLVED" if verification.passed else "ESCALATED"
        print(f"  -> OUTCOME: {final}")
        record.update({
            "final": final, "verifier_ran": True, "verifier_passed": verification.passed,
            "matches_ground_truth": (inv.root_cause.value == c["true_cause"]) if final == "RESOLVED" else None,
        })
        results.append(record)
        print()

    print(f"=== Summary (real generation calls made: {calls_made}) ===")
    header = f"{'order_id':28s} {'bucket':16s} {'model':8s} {'finish_reason':14s} {'latency':>8s} {'schema':7s} {'verifier':9s} {'final':10s} proposed/true"
    print(header)
    for r in results:
        verifier_s = ("PASS" if r.get("verifier_passed") else "FAIL") if r.get("verifier_ran") else "n/a"
        latency_s = f"{r['latency_ms']:.0f}ms" if r.get("latency_ms") is not None else "n/a"
        match_tag = "  [MATCHES GT]" if r.get("matches_ground_truth") else ("  [MISMATCH]" if r.get("matches_ground_truth") is False else "")
        print(f"{r['order_id']:28s} {r['bucket']:16s} {('OK' if r['model_success'] else 'ERR'):8s} "
              f"{str(r.get('finish_reason')):14s} {latency_s:>8s} {str(r.get('schema_valid', False)):7s} "
              f"{verifier_s:9s} {r['final']:10s} {r.get('proposed_cause', '')}/{r['true_cause']}{match_tag}")

    resolved = sum(1 for r in results if r["final"] == "RESOLVED")
    print(f"\n{resolved}/{len(results)} resolved, {len(results) - resolved}/{len(results)} escalated "
          f"(real generation calls: {calls_made}, plus 1 auth-error test if not skipped)")


if __name__ == "__main__":
    main()
