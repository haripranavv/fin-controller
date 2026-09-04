"""LLM client abstraction for root-cause investigation.

RootCauseLLMClient is a narrow Protocol (one method), the same shape as
app.narration.client.NarrationLLMClient, so tests never need a real API
key — see MockRootCauseClient. AnthropicRootCauseClient and
GeminiRootCauseClient are the real implementations, each constructed only
when actually needed; nothing in the test suite requires either to work,
or even imports either at collection time. GeminiRootCauseClient
(gemini-3.6-flash) is the provider scripts/*.py now select first when a
key is configured — see each script's client-selection block — but
AnthropicRootCauseClient is kept, unchanged, as the fallback real
provider; neither this Protocol nor investigator.py/case.py/prompts.py
needed to change for either implementation to plug in.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from app.models.enums import RootCause


class RootCauseLLMClient(Protocol):
    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return raw text — expected to be a JSON object matching
        RootCauseInvestigation's schema, but NOT assumed to be valid; the
        caller (investigator.py) validates it. May raise on transport
        failure — the caller catches broadly and treats it as "don't
        trust this", never lets it crash a case."""
        ...


class MockRootCauseClient:
    """Test double: returns pre-programmed responses in order, or a fixed
    default. Never touches the network. Records every call for assertions."""

    def __init__(self, responses: list[str] | None = None, default: str | None = None) -> None:
        self._responses = list(responses or [])
        self._default = default
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise RuntimeError("MockRootCauseClient: no more programmed responses and no default set")


class RaisingRootCauseClient:
    """Test double that always raises, simulating a transport/API failure."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("simulated API failure")

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        raise self._exc


_INVESTIGATION_TOOL: dict[str, Any] = {
    "name": "record_root_cause_investigation",
    "description": "Record the proposed root cause for a financial divergence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string", "enum": [c.value for c in RootCause]},
            "supporting_evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "explanation": {"type": "string"},
        },
        "required": ["root_cause", "supporting_evidence", "confidence", "explanation"],
    },
}


class AnthropicRootCauseClient:
    """Real implementation — Claude via forced tool-use for structured JSON."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy import: only needed when this class is actually constructed

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=768,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[_INVESTIGATION_TOOL],
            tool_choice={"type": "tool", "name": _INVESTIGATION_TOOL["name"]},
        )
        for block in response.content:
            if block.type == "tool_use":
                return json.dumps(block.input)
        raise RuntimeError("Anthropic response contained no tool_use block")


# Same fields/bounds as _INVESTIGATION_TOOL above, expressed as a Gemini
# response_schema (a constrained subset of OpenAPI 3.0 Schema) rather than
# a tool-call input_schema — the two providers reach the same JSON contract
# by different native mechanisms. root_cause's "enum" here is the actual
# generation-time constraint enforcing section 10's "bounded categories,
# no exceptions"; investigator.py's Pydantic validation (extra="forbid",
# RootCause enum, confidence 0.0-1.0) still runs unchanged afterward and
# remains the real authority — this is a second, earlier layer of the same
# boundedness, not a replacement for it.
_GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "root_cause": {"type": "STRING", "enum": [c.value for c in RootCause]},
        "supporting_evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER", "minimum": 0.0, "maximum": 1.0},
        "explanation": {"type": "STRING"},
    },
    "required": ["root_cause", "supporting_evidence", "confidence", "explanation"],
    "propertyOrdering": ["root_cause", "supporting_evidence", "confidence", "explanation"],
}


class GeminiRootCauseClient:
    """Real implementation — Gemini 3.6 Flash via strict structured output
    (response_mime_type="application/json" + response_schema constraining
    root_cause to app.models.enums.RootCause's bounded set), the same JSON
    contract AnthropicRootCauseClient produces via forced tool-use."""

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai  # lazy import: only needed when this class is actually constructed

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        response = self._client.models.generate_content(
            model=self._model,
            contents=user_prompt,
            config={
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "response_schema": _GEMINI_RESPONSE_SCHEMA,
                # gemini-3.6-flash spends part of max_output_tokens on
                # internal "thinking" before the visible answer, and
                # rejects thinking_config={"thinking_budget": 0} outright
                # (400 INVALID_ARGUMENT - this model cannot disable
                # thinking). 768 was found to systematically truncate the
                # JSON answer mid-string (finish_reason=MAX_TOKENS,
                # thoughts_token_count alone regularly exceeded 768) in
                # 7/8 real cases during a live smoke test against the
                # actual API (scripts/gemini_smoke_test.py). 2048 was
                # verified against the real API to leave ~900 tokens of
                # headroom over actual thinking+answer usage on the same
                # real cases (finish_reason=STOP, complete valid JSON).
                "max_output_tokens": 2048,
            },
        )
        text = response.text
        if not text:
            raise RuntimeError("Gemini response contained no text")
        return text
