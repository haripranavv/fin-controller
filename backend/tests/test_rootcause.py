"""Milestone 8 tests: the root_cause_investigator AI tool. Entirely mocked —
no ANTHROPIC_API_KEY/GEMINI_API_KEY or network access needed anywhere in
this file.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from app.models.enums import RootCause
from app.rootcause.client import _GEMINI_RESPONSE_SCHEMA, _INVESTIGATION_TOOL, MockRootCauseClient, RaisingRootCauseClient
from app.rootcause.investigator import MIN_CONFIDENCE, investigate_root_cause, to_root_cause_proposal
from app.rootcause.types import InvestigationOutcome, RootCauseInvestigation
from app.verifier.checks import verify_root_cause_proposal

ROOTCAUSE_DIR = Path(__file__).resolve().parent.parent / "app" / "rootcause"

SPEC_EXAMPLE = {
    "root_cause": "unreported_fee",
    "supporting_evidence": ["fee_123"],
    "confidence": 0.89,
    "explanation": "bank narration references an additional processing charge",
}


# --- isolation -----------------------------------------------------------


def test_rootcause_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for path in ROOTCAUSE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"


def test_investigation_outcome_carries_no_resolution_information():
    # InvestigationOutcome/RootCauseInvestigation must not be able to
    # represent "the case is resolved" by construction — section 9/10's
    # principle: AI confidence alone never resolves anything.
    forbidden = {"resolved", "accepted", "verified", "case_id"}
    outcome_fields = {f.name for f in InvestigationOutcome.__dataclass_fields__.values()}
    investigation_fields = set(RootCauseInvestigation.model_fields.keys())
    assert not (outcome_fields & forbidden)
    assert not (investigation_fields & forbidden)


# --- spec-literal worked example --------------------------------------------


def test_spec_worked_example_end_to_end():
    client = MockRootCauseClient(default=json.dumps(SPEC_EXAMPLE))
    outcome = investigate_root_cause(client, "settlement", 1_050_000, 1_035_000, -15_000, [
        {"id": "fee_123", "type": "bank_transaction", "amount_paisa": 1_035_000, "narration": "ADDL PROC CHG APPLIED"},
    ])

    assert outcome.error is None
    assert outcome.passed_confidence_gate is True
    inv = outcome.investigation
    assert inv.root_cause.value == "unreported_fee"
    assert inv.supporting_evidence == ["fee_123"]
    assert inv.confidence == 0.89

    proposal = to_root_cause_proposal(inv, -15_000)
    result = verify_root_cause_proposal(proposal, 1_050_000, 1_035_000, {"fee_123"})
    assert result.passed  # replicates section 11's own worked example end to end


def test_prompt_contains_paisa_units_and_bounded_causes():
    client = MockRootCauseClient(default=json.dumps(SPEC_EXAMPLE))
    investigate_root_cause(client, "settlement", 100, 90, -10, [])
    system_prompt, user_prompt = client.calls[0]
    assert "paisa" in system_prompt.lower()
    payload = json.loads(user_prompt)
    assert payload == {
        "divergence_stage": "settlement", "expected_amount": 100, "actual_amount": 90, "delta": -10,
        "evidence": [], "allowed_causes": payload["allowed_causes"],
    }
    assert set(payload["allowed_causes"]) == {
        "duplicate_refund", "missing_refund_netting", "unreported_fee", "partial_settlement_split",
        "currency_rounding", "duplicate_bank_credit", "unmatched_external_deduction", "unknown",
    }


# --- confidence gate -----------------------------------------------------------


def test_confidence_at_or_above_threshold_passes_gate():
    payload = {**SPEC_EXAMPLE, "confidence": MIN_CONFIDENCE}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.passed_confidence_gate is True


def test_confidence_below_threshold_fails_gate_but_investigation_still_present():
    payload = {**SPEC_EXAMPLE, "confidence": 0.40}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.error is None
    assert outcome.investigation is not None
    assert outcome.passed_confidence_gate is False


def test_unknown_cause_at_low_confidence_correctly_escalates():
    payload = {"root_cause": "unknown", "supporting_evidence": [], "confidence": 0.2, "explanation": "insufficient evidence"}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.passed_confidence_gate is False


# --- schema validation (must never crash the caller) -----------------------------


def test_invalid_json_response_handled_gracefully():
    client = MockRootCauseClient(default="not json {{{")
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None
    assert "invalid JSON" in outcome.error
    assert outcome.passed_confidence_gate is False


def test_unbounded_root_cause_rejected_by_schema():
    # section 10: "The AI cannot invent new cause categories" — enforced at
    # the schema layer, before the confidence gate even runs.
    payload = {**SPEC_EXAMPLE, "root_cause": "the_dog_ate_my_ledger"}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None
    assert "schema validation failed" in outcome.error


def test_schema_violation_wrong_confidence_type_handled_gracefully():
    payload = {**SPEC_EXAMPLE, "confidence": "very confident"}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None
    assert outcome.error is not None


def test_schema_violation_confidence_out_of_range_handled_gracefully():
    payload = {**SPEC_EXAMPLE, "confidence": 1.2}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None


def test_schema_violation_extra_unexpected_field_rejected():
    payload = {**SPEC_EXAMPLE, "claimed_adjustment_paisa": -15000}  # AI cannot smuggle in a numeric override
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None
    assert outcome.error is not None


def test_missing_required_field_handled_gracefully():
    payload = {"root_cause": "unreported_fee"}  # missing confidence
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None
    assert outcome.error is not None


def test_empty_supporting_evidence_is_schema_valid():
    payload = {**SPEC_EXAMPLE, "supporting_evidence": []}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.error is None
    assert outcome.investigation.supporting_evidence == []


# --- transport failure -----------------------------------------------------------


def test_client_transport_failure_handled_gracefully():
    client = RaisingRootCauseClient(ConnectionError("simulated network failure"))
    outcome = investigate_root_cause(client, "settlement", 100, 90, -10, [])
    assert outcome.investigation is None
    assert "LLM call failed" in outcome.error
    assert outcome.passed_confidence_gate is False


# --- to_root_cause_proposal --------------------------------------------------------


def test_to_root_cause_proposal_uses_given_delta_not_a_parsed_amount():
    # section 10's output schema has no numeric adjustment field — the
    # given delta becomes the claimed adjustment, always.
    payload = {**SPEC_EXAMPLE, "confidence": 0.9}
    client = MockRootCauseClient(default=json.dumps(payload))
    outcome = investigate_root_cause(client, "settlement", 1000, 850, -150, [])
    proposal = to_root_cause_proposal(outcome.investigation, -150)
    assert proposal.claimed_adjustment_paisa == -150
    assert proposal.root_cause == "unreported_fee"
    assert proposal.confidence == 0.9


# --- GeminiRootCauseClient (real client - schema/shape only, no network) ----------


def test_gemini_response_schema_bounds_root_cause_to_the_closed_enum():
    # The generation-time constraint claiming "bounded root-cause
    # categories" for Gemini specifically - must be the exact same set
    # investigator.py's Pydantic validation (the real authority) enforces,
    # never a hand-drifted copy.
    allowed = {c.value for c in RootCause}
    assert set(_GEMINI_RESPONSE_SCHEMA["properties"]["root_cause"]["enum"]) == allowed
    assert _GEMINI_RESPONSE_SCHEMA["required"] == ["root_cause", "supporting_evidence", "confidence", "explanation"]


def test_gemini_and_anthropic_schemas_agree_on_the_same_bounded_set():
    # Two providers, two native mechanisms (Gemini response_schema vs.
    # Anthropic forced tool-use input_schema) - same closed set either way.
    gemini_causes = set(_GEMINI_RESPONSE_SCHEMA["properties"]["root_cause"]["enum"])
    anthropic_causes = set(_INVESTIGATION_TOOL["input_schema"]["properties"]["root_cause"]["enum"])
    assert gemini_causes == anthropic_causes


def test_rootcause_client_module_does_not_eagerly_import_provider_sdks():
    # AnthropicRootCauseClient/GeminiRootCauseClient both lazy-import their
    # SDK inside __init__ (see client.py's docstrings) - importing the
    # module itself, and using MockRootCauseClient/RaisingRootCauseClient,
    # must never require anthropic or google.genai to be installed.
    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "rootcause" / "client.py").read_text(encoding="utf-8"))
    top_level_imports = set()
    for node in tree.body:  # module-level only, not inside function bodies
        if isinstance(node, ast.Import):
            top_level_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module)
    assert "anthropic" not in top_level_imports
    assert "google" not in top_level_imports
    assert "google.genai" not in top_level_imports
