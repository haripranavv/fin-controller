"""Enforces PROJECT_SPEC.md section 6's DIVERGENCE_TRACE -> "known cause?"
-> [NO] -> ROOT_CAUSE_INVESTIGATE flow as an actual code path, not just a
convention callers are expected to follow.

Tries app.pipeline.known_causes.detect_known_cause FIRST (unchanged — the
deterministic rule table from milestone 6). The AI investigator is called
ONLY when that returns None on a genuine, concrete divergence — never for
a clean chain, never for an "unresolved" (missing-evidence) trace where
there is nothing to investigate, and never when a deterministic rule
already explains it (avoiding a wasted, inappropriate AI call).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.datagen.models import GenBankTransaction, GenRefund
from app.divergence.types import StageResult
from app.pipeline.known_causes import detect_known_cause
from app.rootcause.client import RootCauseLLMClient
from app.rootcause.evidence import build_evidence
from app.rootcause.investigator import investigate_root_cause, to_root_cause_proposal
from app.verifier.types import RootCauseProposal


@dataclass
class CaseInvestigationResult:
    proposal: RootCauseProposal | None
    source: str  # "deterministic" | "ai" | "none"
    detail: str


def investigate_case(
    client: RootCauseLLMClient,
    first_divergence: StageResult,
    group_refunds: list[GenRefund],
    bank_txns: list[GenBankTransaction],
) -> CaseInvestigationResult:
    if first_divergence.delta_paisa is None:
        return CaseInvestigationResult(None, "none", "no concrete divergence to investigate (missing evidence)")

    known = detect_known_cause(first_divergence, group_refunds, bank_txns)
    if known is not None:
        return CaseInvestigationResult(known, "deterministic", f"known cause '{known.root_cause}' — AI not invoked")

    evidence = build_evidence(group_refunds, bank_txns)
    outcome = investigate_root_cause(
        client, first_divergence.stage, first_divergence.expected_paisa,
        first_divergence.actual_paisa, first_divergence.delta_paisa, evidence,
    )
    if outcome.error is not None:
        return CaseInvestigationResult(None, "none", f"AI investigation failed: {outcome.error}")
    if not outcome.passed_confidence_gate:
        return CaseInvestigationResult(
            None, "none",
            f"AI proposed '{outcome.investigation.root_cause.value}' at confidence "
            f"{outcome.investigation.confidence:.2f} — below the {0.60:.2f} gate",
        )

    proposal = to_root_cause_proposal(outcome.investigation, first_divergence.delta_paisa)
    return CaseInvestigationResult(proposal, "ai", f"AI proposed '{proposal.root_cause}' at confidence {proposal.confidence:.2f}")
