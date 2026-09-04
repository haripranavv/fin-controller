"""Verifier input/output types.

VerificationResult's shape (a list of named, individually-explained
CheckResults) is deliberately audit-friendly: it's meant to serialize
directly into AgentEvent.verifier_result (a JSON column — see
app/models/operational.py) once the orchestrator exists, so a later
"why did this case escalate" view can show every check that ran, not just
a final boolean.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class VerificationResult:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    @classmethod
    def combine(cls, checks: list[CheckResult]) -> VerificationResult:
        return cls(passed=all(c.passed for c in checks), checks=checks)

    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


@dataclass
class RootCauseProposal:
    """What an AI root_cause_investigator (a later milestone) would hand
    the verifier — mirrors PROJECT_SPEC.md section 10's output contract
    (root_cause, supporting_evidence, confidence), minus `explanation`
    (free text the verifier has no use for).

    claimed_adjustment_paisa is SIGNED: the verifier checks
    `expected_paisa + claimed_adjustment_paisa == actual_paisa` (within
    tolerance) — section 11's own example is
    `10,500 + (-150) = 10,350`, a fee (negative/deduction) adjustment.
    """

    root_cause: str
    claimed_adjustment_paisa: int
    confidence: float
    supporting_evidence_ids: list[str]
