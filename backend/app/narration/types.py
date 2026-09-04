"""narration_extractor input/output types.

NarrationExtraction mirrors PROJECT_SPEC.md section 9's output schema
exactly:

    {
      "counterparty": "string|null",
      "reference_id": "string|null",
      "amount_hint": "number|null",
      "transaction_type": "payment|refund|settlement|unknown",
      "flags": [],
      "confidence": 0.0
    }

amount_hint is integer PAISA, for consistency with every other monetary
field in this codebase (section 4: "All monetary values are integer
paisa") — section 9's worked example uses the same numeral for input
`amount` and output `amount_hint` without specifying a unit conversion, so
paisa-in/paisa-out is the natural, consistent reading. `strict` mode
(extra="forbid", confidence bounded to [0,1]) is the "must be schema
validated" requirement from section 9, enforced by Pydantic on
construction — see app/narration/extractor.py for how a validation failure
is handled (never raised past the caller; always converted into an
ExtractionOutcome the confidence gate can reject).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NarrationExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty: str | None = None
    reference_id: str | None = None
    amount_hint: int | None = Field(default=None, ge=0)
    transaction_type: Literal["payment", "refund", "settlement", "unknown"] = "unknown"
    flags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class ExtractionOutcome:
    """Wraps one narration_extractor call. Deliberately never raises past
    the caller (section 2: AI output is always untrusted until validated —
    an unparseable/invalid response is just another kind of "don't trust
    this", not a crash). `extraction` is None whenever `error` is set;
    `passed_confidence_gate` is only ever True when `extraction` is a
    valid, schema-checked NarrationExtraction with confidence >= the
    section 9 threshold.

    Deliberately carries NO match/settlement information — this type
    cannot represent a match decision by construction. That decision is
    app.narration.rematch's job, using this as input, never the reverse.
    """

    extraction: NarrationExtraction | None
    raw_response: str
    error: str | None
    passed_confidence_gate: bool
