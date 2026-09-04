"""root_cause_investigator tool (PROJECT_SPEC.md section 7 & 10).

Proposes a bounded root cause for a divergence, given only the numbers and
evidence already established by deterministic tracing. Output is
schema-validated (section 10 implies the same "must be schema validated"
discipline as section 9) and gated on confidence (section 10's suggested
gate: "< 0.60 -> escalate; >= 0.60 -> send to verifier"). This function
NEVER resolves a case itself — see to_root_cause_proposal() and
app/rootcause/case.py for how a passing investigation still has to clear
app.verifier.checks.verify_root_cause_proposal (unchanged) before anything
is treated as resolved.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.rootcause.client import RootCauseLLMClient
from app.rootcause.prompts import SYSTEM_PROMPT, build_user_prompt
from app.rootcause.types import InvestigationOutcome, RootCauseInvestigation
from app.verifier.types import RootCauseProposal

MIN_CONFIDENCE = 0.60  # section 10's suggested confidence gate


def investigate_root_cause(
    client: RootCauseLLMClient,
    divergence_stage: str,
    expected_paisa: int,
    actual_paisa: int,
    delta_paisa: int,
    evidence: list[dict],
) -> InvestigationOutcome:
    user_prompt = build_user_prompt(divergence_stage, expected_paisa, actual_paisa, delta_paisa, evidence)

    try:
        raw = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
    except Exception as exc:  # noqa: BLE001 — a transport/API failure is just another "don't trust this"
        return InvestigationOutcome(investigation=None, raw_response="", error=f"LLM call failed: {exc}", passed_confidence_gate=False)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return InvestigationOutcome(investigation=None, raw_response=raw, error=f"invalid JSON: {exc}", passed_confidence_gate=False)

    try:
        investigation = RootCauseInvestigation.model_validate(parsed)
    except ValidationError as exc:
        return InvestigationOutcome(investigation=None, raw_response=raw, error=f"schema validation failed: {exc}", passed_confidence_gate=False)

    return InvestigationOutcome(
        investigation=investigation,
        raw_response=raw,
        error=None,
        passed_confidence_gate=investigation.confidence >= MIN_CONFIDENCE,
    )


def to_root_cause_proposal(investigation: RootCauseInvestigation, delta_paisa: int) -> RootCauseProposal:
    """Converts a valid, confidence-gated AI investigation into the same
    RootCauseProposal shape app.pipeline.known_causes' deterministic rules
    produce, for verify_root_cause_proposal to independently check.

    claimed_adjustment_paisa is set to the delta the investigator was GIVEN
    as input, not something parsed out of its output — section 10's output
    schema has no numeric adjustment field, because the investigator's job
    is choosing which bounded label explains an already-known gap, not
    re-deriving an amount (see app/rootcause/types.py's module docstring).
    """
    return RootCauseProposal(
        root_cause=investigation.root_cause.value,
        claimed_adjustment_paisa=delta_paisa,
        confidence=investigation.confidence,
        supporting_evidence_ids=investigation.supporting_evidence,
    )
