"""LLM client abstraction for narration extraction.

NarrationLLMClient is a narrow Protocol (one method) specifically so tests
never need a real API key — see MockNarrationClient. AnthropicNarrationClient
is the real implementation, constructed only when actually needed (e.g. by
scripts/run_narration_eval.py when ANTHROPIC_API_KEY is set); nothing in
the test suite requires it to work, or even imports it at collection time.
"""
from __future__ import annotations

import json
from typing import Any, Protocol


class NarrationLLMClient(Protocol):
    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return raw text — expected to be a JSON object matching
        NarrationExtraction's schema, but NOT assumed to be valid; the
        caller (extractor.py) validates it. May raise on transport failure
        (network error, auth failure, etc.) — the caller catches broadly
        and treats it as "don't trust this", never lets it crash a case."""
        ...


class MockNarrationClient:
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
        raise RuntimeError("MockNarrationClient: no more programmed responses and no default set")


class RaisingNarrationClient:
    """Test double that always raises, simulating a transport/API failure."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("simulated API failure")

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        raise self._exc


_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_narration_extraction",
    "description": "Record the structured extraction of a financial narration.",
    "input_schema": {
        "type": "object",
        "properties": {
            "counterparty": {"type": ["string", "null"]},
            "reference_id": {"type": ["string", "null"]},
            "amount_hint": {"type": ["integer", "null"], "description": "paisa, not rupees"},
            "transaction_type": {"type": "string", "enum": ["payment", "refund", "settlement", "unknown"]},
            "flags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["transaction_type", "flags", "confidence"],
    },
}


class AnthropicNarrationClient:
    """Real implementation — Claude via forced tool-use for structured JSON."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy import: only needed when this class is actually constructed

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": _EXTRACTION_TOOL["name"]},
        )
        for block in response.content:
            if block.type == "tool_use":
                return json.dumps(block.input)
        raise RuntimeError("Anthropic response contained no tool_use block")
