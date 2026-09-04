"""narration_extractor tool (PROJECT_SPEC.md section 7 & 9).

Converts messy/unseen narration into structured fields. Output is
schema-validated (section 9: "The output must be schema validated") and
gated on confidence (section 9's suggested gate: "< 0.50 -> escalate;
>= 0.50 -> allow deterministic re-match"). This function NEVER creates a
match itself (section 9: "AI confidence alone never creates a match") —
see app/narration/rematch.py for the deterministic step that actually
does.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.narration.client import NarrationLLMClient
from app.narration.prompts import SYSTEM_PROMPT, build_user_prompt
from app.narration.types import ExtractionOutcome, NarrationExtraction

MIN_CONFIDENCE = 0.50  # section 9's suggested confidence gate


def extract_narration(client: NarrationLLMClient, narration: str, amount_paisa: int, date: str) -> ExtractionOutcome:
    user_prompt = build_user_prompt(narration, amount_paisa, date)

    try:
        raw = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
    except Exception as exc:  # noqa: BLE001 — a transport/API failure is just another "don't trust this"
        return ExtractionOutcome(extraction=None, raw_response="", error=f"LLM call failed: {exc}", passed_confidence_gate=False)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ExtractionOutcome(extraction=None, raw_response=raw, error=f"invalid JSON: {exc}", passed_confidence_gate=False)

    try:
        extraction = NarrationExtraction.model_validate(parsed)
    except ValidationError as exc:
        return ExtractionOutcome(extraction=None, raw_response=raw, error=f"schema validation failed: {exc}", passed_confidence_gate=False)

    return ExtractionOutcome(
        extraction=extraction,
        raw_response=raw,
        error=None,
        passed_confidence_gate=extraction.confidence >= MIN_CONFIDENCE,
    )
