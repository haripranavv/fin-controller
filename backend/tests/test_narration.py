"""Milestone 7 tests: the narration_extractor AI tool. Entirely mocked —
no ANTHROPIC_API_KEY or network access needed anywhere in this file.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from app.narration.client import MockNarrationClient, RaisingNarrationClient
from app.narration.extractor import MIN_CONFIDENCE, extract_narration
from app.narration.types import ExtractionOutcome, NarrationExtraction

NARRATION_DIR = Path(__file__).resolve().parent.parent / "app" / "narration"

SPEC_EXAMPLE = {
    "counterparty": "Raj Trading Co",
    "reference_id": "INV88213",
    "amount_hint": 12000,
    "transaction_type": "payment",
    "flags": ["partial"],
    "confidence": 0.87,
}


# --- isolation -----------------------------------------------------------


def test_narration_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in NARRATION_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


def test_extraction_outcome_carries_no_match_information():
    # ExtractionOutcome/NarrationExtraction must not be able to represent a
    # match decision by construction — section 9: "AI confidence alone
    # never creates a match".
    forbidden_field_names = {"match", "settlement_id", "accepted", "target_id", "source_id"}
    outcome_fields = {f.name for f in ExtractionOutcome.__dataclass_fields__.values()}
    extraction_fields = set(NarrationExtraction.model_fields.keys())
    assert not (outcome_fields & forbidden_field_names)
    assert not (extraction_fields & forbidden_field_names)


# --- spec-literal worked example --------------------------------------------


def test_spec_worked_example_end_to_end():
    client = MockNarrationClient(default=json.dumps(SPEC_EXAMPLE))
    outcome = extract_narration(client, "NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL", 12000, "2026-08-30")

    assert outcome.error is None
    assert outcome.passed_confidence_gate is True
    e = outcome.extraction
    assert e.counterparty == "Raj Trading Co"
    assert e.reference_id == "INV88213"
    assert e.amount_hint == 12000
    assert e.transaction_type == "payment"
    assert e.flags == ["partial"]
    assert e.confidence == 0.87


def test_prompt_sent_to_client_contains_narration_amount_and_date():
    client = MockNarrationClient(default=json.dumps(SPEC_EXAMPLE))
    extract_narration(client, "NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL", 12000, "2026-08-30")
    system_prompt, user_prompt = client.calls[0]
    payload = json.loads(user_prompt)
    assert payload == {"narration": "NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL", "amount": 12000, "date": "2026-08-30"}
    assert "paisa" in system_prompt.lower()


# --- confidence gate -----------------------------------------------------------


def test_confidence_at_or_above_threshold_passes_gate():
    payload = {**SPEC_EXAMPLE, "confidence": MIN_CONFIDENCE}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.passed_confidence_gate is True


def test_confidence_below_threshold_fails_gate_but_extraction_still_present():
    payload = {**SPEC_EXAMPLE, "confidence": 0.30}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.error is None
    assert outcome.extraction is not None  # a low-confidence extraction is still a VALID extraction
    assert outcome.passed_confidence_gate is False


def test_zero_confidence_fails_gate():
    payload = {**SPEC_EXAMPLE, "confidence": 0.0}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.passed_confidence_gate is False


# --- schema validation (must never crash the caller) -----------------------------


def test_invalid_json_response_handled_gracefully():
    client = MockNarrationClient(default="this is not json at all {{{")
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert outcome.error is not None
    assert "invalid JSON" in outcome.error
    assert outcome.passed_confidence_gate is False


def test_schema_violation_wrong_confidence_type_handled_gracefully():
    payload = {**SPEC_EXAMPLE, "confidence": "very confident"}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert "schema validation failed" in outcome.error


def test_schema_violation_confidence_out_of_range_handled_gracefully():
    payload = {**SPEC_EXAMPLE, "confidence": 1.5}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert outcome.error is not None


def test_schema_violation_bad_transaction_type_handled_gracefully():
    payload = {**SPEC_EXAMPLE, "transaction_type": "bogus_type"}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert outcome.error is not None


def test_schema_violation_extra_unexpected_field_rejected():
    payload = {**SPEC_EXAMPLE, "root_cause": "unreported_fee"}  # AI cannot smuggle in a root-cause proposal here
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert outcome.error is not None


def test_negative_amount_hint_rejected():
    payload = {**SPEC_EXAMPLE, "amount_hint": -500}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None


def test_null_amount_hint_is_valid():
    payload = {**SPEC_EXAMPLE, "amount_hint": None}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.error is None
    assert outcome.extraction.amount_hint is None


def test_null_counterparty_and_reference_are_valid():
    payload = {**SPEC_EXAMPLE, "counterparty": None, "reference_id": None, "amount_hint": None}
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.error is None
    assert outcome.extraction.counterparty is None
    assert outcome.extraction.reference_id is None


def test_missing_required_field_handled_gracefully():
    payload = {"counterparty": "X"}  # missing confidence, transaction_type
    client = MockNarrationClient(default=json.dumps(payload))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert outcome.error is not None


# --- transport failure -----------------------------------------------------------


def test_client_transport_failure_handled_gracefully():
    client = RaisingNarrationClient(ConnectionError("simulated network failure"))
    outcome = extract_narration(client, "x", 100, "2026-01-01")
    assert outcome.extraction is None
    assert "LLM call failed" in outcome.error
    assert outcome.passed_confidence_gate is False
