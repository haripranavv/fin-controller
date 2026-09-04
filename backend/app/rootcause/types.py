"""root_cause_investigator input/output types.

RootCauseInvestigation mirrors PROJECT_SPEC.md section 10's output schema
exactly:

    {
      "root_cause": "unreported_fee",
      "supporting_evidence": ["fee_123"],
      "confidence": 0.89,
      "explanation": "..."
    }

`root_cause` is typed as app.models.enums.RootCause itself (not a
re-declared Literal) — the SAME bounded set used by the DB CHECK
constraint, app.pipeline.known_causes, and app.verifier.checks — so there
is exactly one place that set is ever defined. Pydantic validates the
incoming string against the enum automatically; an unbounded value (section
10: "The AI cannot invent new cause categories") fails schema validation
before it ever reaches the verifier.

Note what this schema does NOT contain: a numeric adjustment. Section 10's
own input contract already hands the investigator `delta` directly — its
job is choosing which bounded LABEL explains an already-known gap, not
independently re-deriving an amount. See investigator.py's
to_root_cause_proposal() for how the given delta becomes
RootCauseProposal.claimed_adjustment_paisa.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RootCause


class RootCauseInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: RootCause
    supporting_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str = ""


@dataclass
class InvestigationOutcome:
    """Wraps one root_cause_investigator call. Never raises past the
    caller (section 2: AI output is always untrusted until validated).
    Carries NO resolution/verification information — this type cannot
    represent "the case is resolved" by construction. Turning a valid,
    confidence-gated investigation into an actual resolution is
    app.verifier.checks.verify_root_cause_proposal's job, via
    investigator.py's to_root_cause_proposal(), never this module's.
    """

    investigation: RootCauseInvestigation | None
    raw_response: str
    error: str | None
    passed_confidence_gate: bool
