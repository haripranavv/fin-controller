#!/usr/bin/env python
"""Milestone 7 evaluation: does narration-extraction-assisted re-match
measurably improve on the deterministic-only baseline, and specifically —
does it help more on UNSEEN (messy) narration formats than on KNOWN
(clean) ones?

Like the other scripts/run_*_report.py scripts, this is a standalone
diagnostic, not the real evaluation harness (milestone 10). It is the one
place outside app.datagen.persist allowed to read ground truth, and only
for the known-vs-unseen category breakdown — app.narration itself never
does (see app/narration/__init__.py).

Uses the REAL Anthropic API if ANTHROPIC_API_KEY is set (via
app.narration.client.AnthropicNarrationClient), otherwise falls back to a
rule-based stand-in extractor and says so clearly — this script must be
runnable and honest either way.

Usage (run from backend/, with the venv active, Postgres up and a dataset
generated):
    python scripts/run_narration_eval.py --dataset-version dev-v1
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.datagen import catalog  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.matcher.db_adapter import load_dataset  # noqa: E402
from app.matcher.reconciler import run_deterministic_matching  # noqa: E402
from app.models.groundtruth import GroundTruth  # noqa: E402
from app.narration.client import AnthropicNarrationClient, NarrationLLMClient  # noqa: E402
from app.narration.extractor import extract_narration  # noqa: E402
from app.narration.rematch import attempt_rematch  # noqa: E402
from app.narration.types import NarrationExtraction  # noqa: E402

_TOKEN_TO_MERCHANT = {catalog.token(name): name for _mid, name in catalog.MERCHANTS}


def _reference_extract(narration: str) -> NarrationExtraction:
    """Rule-based stand-in used only when no ANTHROPIC_API_KEY is set —
    parses THIS project's own narration formats independently of any
    generator internals, as a fallback so this script stays runnable and
    demonstrates the mechanism without a live key. Real usage should
    prefer AnthropicNarrationClient (see main() below)."""
    if not narration:
        return NarrationExtraction(confidence=0.0, transaction_type="unknown")
    upper = narration.upper()
    counterparty = next((name for token, name in _TOKEN_TO_MERCHANT.items() if token in upper), None)
    ref_match = re.search(r"(?:INV|REF)\s*([A-Z0-9]+)", upper)
    reference_id = ref_match.group(1) if ref_match else None
    amount_hint = None
    imps_match = re.match(r"^IMPS:[A-Z0-9]+:[A-Z0-9]+:(\d+)$", upper)
    if imps_match:
        amount_hint = int(imps_match.group(1)) * 100
    flags = ["partial"] if "PARTIAL" in upper else []
    confidence = 0.85 if counterparty else 0.55
    return NarrationExtraction(counterparty=counterparty, reference_id=reference_id, amount_hint=amount_hint,
                                transaction_type="payment", flags=flags, confidence=confidence)


class _ReferenceStandInClient:
    """Wraps _reference_extract to satisfy NarrationLLMClient, so the same
    extract_narration()/schema-validation path used everywhere else runs
    here too — this script exercises the real pipeline shape, not a
    shortcut around it."""

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        import json

        payload = json.loads(user_prompt)
        extraction = _reference_extract(payload["narration"])
        return extraction.model_dump_json()


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

    if settings.anthropic_api_key:
        client: NarrationLLMClient = AnthropicNarrationClient(settings.anthropic_api_key, settings.anthropic_model)
        mode = f"REAL Anthropic API ({settings.anthropic_model})"
    else:
        client = _ReferenceStandInClient()
        mode = "SIMULATED - no ANTHROPIC_API_KEY set, using a rule-based stand-in, NOT a real LLM call"

    print(f"=== Milestone 7 narration extraction evaluation: {dv} ===")
    print(f"extractor mode: {mode}")
    print()

    baseline = run_deterministic_matching(orders, payments, refunds, settlements, bank_txns)
    baseline_accepted = [m for m in baseline.settlement_payment if m.accepted]
    consumed = {m.source_id for m in baseline_accepted}

    payments_by_id = {p.payment_id: p for p in payments}
    order_by_payment = {p.payment_id: p.order_id for p in payments}
    gt_by_order = {g.record_id: g for g in ground_truth}

    def category_of(payment_id: str) -> str:
        gt = gt_by_order.get(order_by_payment.get(payment_id, ""))
        if gt is None:
            return "unknown"
        return gt.injected_noise_type.split("+")[0]

    baseline_matched_by_category: Counter = Counter()
    total_by_category: Counter = Counter()
    for p in payments:
        total_by_category[category_of(p.payment_id)] += 1
        if p.payment_id in consumed:
            baseline_matched_by_category[category_of(p.payment_id)] += 1

    unmatched_ids = [p.payment_id for p in payments if p.payment_id not in consumed]

    recovered_by_category: Counter = Counter()
    attempted_extractions = 0
    gate_failures = 0
    schema_or_transport_failures = 0

    for pid in unmatched_ids:
        payment = payments_by_id[pid]
        if not payment.narration:
            continue
        attempted_extractions += 1
        outcome = extract_narration(client, payment.narration, payment.amount_paisa, payment.created_at.date().isoformat())
        if outcome.error is not None:
            schema_or_transport_failures += 1
            continue
        if not outcome.passed_confidence_gate:
            gate_failures += 1
            continue
        result = attempt_rematch(pid, outcome.extraction, orders, payments, refunds, settlements, already_consumed_payment_ids=consumed)
        if result is not None:
            consumed.add(pid)
            recovered_by_category[category_of(pid)] += 1

    print(f"Unmatched payments after deterministic-only baseline: {len(unmatched_ids)}")
    print(f"  with narration, extraction attempted:   {attempted_extractions}")
    print(f"  schema/transport failures:              {schema_or_transport_failures}")
    print(f"  failed confidence gate (< 0.50):        {gate_failures}")
    print(f"  recovered via narration-assisted re-match: {sum(recovered_by_category.values())}")
    print()

    print("Match rate by category - baseline vs. after narration-assisted re-match:")
    print(f"  {'category':30s} {'total':>6s} {'baseline':>10s} {'+AI':>10s} {'lift':>8s}")
    for cat in sorted(total_by_category):
        total = total_by_category[cat]
        base = baseline_matched_by_category[cat]
        after = base + recovered_by_category[cat]
        lift = (after - base) / total * 100 if total else 0.0
        print(f"  {cat:30s} {total:6d} {base / total:9.1%} {after / total:9.1%} {lift:+7.1f}pp")

    print()
    print("Known vs unseen narration formats (the section 16 comparison):")
    known = total_by_category.get("clean", 0)
    known_base = baseline_matched_by_category.get("clean", 0)
    known_after = known_base + recovered_by_category.get("clean", 0)
    unseen = total_by_category.get("messy_narration", 0)
    unseen_base = baseline_matched_by_category.get("messy_narration", 0)
    unseen_after = unseen_base + recovered_by_category.get("messy_narration", 0)
    if known:
        print(f"  known (clean) format:   baseline {known_base}/{known} = {known_base / known:.1%}  "
              f"-> +AI {known_after}/{known} = {known_after / known:.1%}")
    if unseen:
        print(f"  unseen (messy) format:  baseline {unseen_base}/{unseen} = {unseen_base / unseen:.1%}  "
              f"-> +AI {unseen_after}/{unseen} = {unseen_after / unseen:.1%}")


if __name__ == "__main__":
    main()
