"""Top-level entry point: the MATCHED -> VERIFY step of PROJECT_SPEC.md
section 6's state machine.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from app.verifier.checks import (
    DEFAULT_TOLERANCE_PAISA,
    MatchLike,
    verify_chronology,
    verify_reconciliation,
    verify_relationship,
)
from app.verifier.types import VerificationResult


def verify_match(
    match: MatchLike,
    expected_paisa: int,
    actual_paisa: int,
    *,
    tolerance_paisa: int = DEFAULT_TOLERANCE_PAISA,
    chronology_events: Sequence[tuple[str, datetime]] | None = None,
) -> VerificationResult:
    """Decide PASS or FAIL for one accepted match given the expected/actual
    amounts it should reconcile (a future divergence engine computes these
    for a real chain hop; callers here — this milestone's own tests —
    supply them directly).

    Applies IDENTICALLY regardless of match.method. There is no special
    code path for a NARRATION_AI_ASSISTED match — that uniformity is
    section 11's "AI-derived match consistency" requirement: an AI-assisted
    match earns no leniency just because AI proposed it (section 2: "AI
    output is always untrusted until deterministic verification succeeds").
    """
    checks = [
        verify_relationship(match.source_type, match.target_type),
        verify_reconciliation(expected_paisa, actual_paisa, tolerance_paisa=tolerance_paisa),
    ]
    if chronology_events:
        checks.append(verify_chronology(chronology_events))
    return VerificationResult.combine(checks)
