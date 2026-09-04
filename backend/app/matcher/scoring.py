"""Deterministic score combination (PROJECT_SPEC.md section 8.6). Every
threshold here is a fixed constant chosen and documented in code — never
something an LLM picks (section 8.6: "Do not let an LLM choose the final
threshold").
"""
from __future__ import annotations

from datetime import datetime

# Accept a settlement<->payment-subset candidate at or above this combined
# amount score. Deliberately generous: a group whose members are genuinely
# correct but whose settlement total is wrong because of a settlement-stage
# divergence (missing_refund_netting, unreported_fee, etc. — see
# app/datagen/settlement.py) should still reach MATCHED here; VERIFY (a
# later milestone) is what actually judges correctness, not the matcher.
SETTLEMENT_MATCH_ACCEPT_THRESHOLD = 0.55

# Accept a settlement<->bank fallback (non-reference) candidate at or above
# this combined amount+date score.
BANK_FALLBACK_ACCEPT_THRESHOLD = 0.5

# If the #2 closest-sum candidate's |delta| is within this many paisa of the
# #1 candidate's, the settlement<->payment match is flagged ambiguous
# (section 8.7: "stop when... evidence is too ambiguous") rather than
# silently accepting a coin-flip.
AMBIGUITY_MARGIN_PAISA = 50


def amount_score(delta_paisa: int, target_paisa: int) -> float:
    """1.0 for an exact match, decaying linearly to 0.0 by the time |delta|
    reaches the full target amount — or a ₹5 floor for very small targets,
    so tiny settlements aren't held to an unreasonably tight absolute
    tolerance.

    Deliberately generous (scale = target, not half of it): a
    missing_refund_netting/duplicate_refund settlement's delta is exactly
    one refund amount, which for a small settlement group can legitimately
    be 30-70% of the target (see app/datagen/flows.py's refund_partial
    range) — the payment membership itself is still correct even though the
    settlement's declared total is wrong (that's the whole point of the
    scenario), and should still reach MATCHED so VERIFY (a later milestone)
    is what catches the discrepancy, not this threshold. A tighter half-
    target scale left ~80% of missing_refund_netting cases below threshold
    on the heldout-v1 dataset — see docs/ARCHITECTURE_NOTES.md.
    """
    scale = max(abs(target_paisa), 500)
    return max(0.0, 1.0 - abs(delta_paisa) / scale)


def date_proximity_score(
    when: datetime, window_start: datetime, window_end: datetime, decay_days: float = 10.0
) -> float:
    """1.0 anywhere inside [window_start, window_end], decaying linearly to
    0.0 over `decay_days` once outside it in either direction."""
    if window_start <= when <= window_end:
        return 1.0
    if when < window_start:
        days_outside = (window_start - when).total_seconds() / 86400
    else:
        days_outside = (when - window_end).total_seconds() / 86400
    return max(0.0, 1.0 - days_outside / decay_days)
