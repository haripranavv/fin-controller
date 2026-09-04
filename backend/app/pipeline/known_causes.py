"""Deterministic "known cause" rule table — PROJECT_SPEC.md section 6's
DIVERGENCE_TRACE -> "known cause?" decision point.

A small, closed set of patterns that explain a divergence WITHOUT needing
AI. This is the rule table flagged as a design gap in
docs/ARCHITECTURE_NOTES.md's milestone 1 notes (item 3) and scoped out in
milestone 2's generator design table ("which axis-B scenarios are
deterministically explainable") — see that table for the full reasoning on
which scenarios belong here and which deliberately don't:

- missing_refund_netting / duplicate_refund: |delta| exactly equals a
  refund amount already in the group's records; sign disambiguates which.
- currency_rounding: |delta| is tiny (magnitude-only rule).
- duplicate_bank_credit: structural — exactly two bank transactions, and
  their sum is exactly double the expected amount.

Deliberately NOT covered (by design — these genuinely need evidence a
numeric/structural rule can't see, e.g. a narration hint, or don't even
have a candidate explanation to check): unreported_fee,
unmatched_external_deduction, ambiguous_cause, partial_settlement_split.
A case that doesn't match any rule here returns None and must escalate in
this deterministic-only milestone (root_cause_investigator, the AI tool
that would otherwise handle it, doesn't exist yet).

Produces a RootCauseProposal (app.verifier.types) with confidence=1.0 —
a deterministic rule is certain by construction, unlike an AI proposal —
so it goes through app.verifier.verify_root_cause_proposal UNCHANGED,
exactly the same verification path a future AI-derived proposal will use.
"""
from __future__ import annotations

from app.datagen.models import GenBankTransaction, GenRefund
from app.divergence.types import StageResult
from app.verifier.types import RootCauseProposal

# Matches app.datagen.settlement's currency_rounding band (rng.randint(1, 5)
# paisa) — see that module's docstring. In a system built against real
# (non-synthetic) data this would be a general small-value business rule
# rather than tuned to the generator; documented here rather than hidden.
CURRENCY_ROUNDING_MAX_PAISA = 5


def detect_known_cause(
    first_divergence: StageResult,
    group_refunds: list[GenRefund],
    bank_txns: list[GenBankTransaction],
) -> RootCauseProposal | None:
    if first_divergence.delta_paisa is None:
        return None  # missing evidence (e.g. no bank txn) — nothing to pattern-match against

    delta = first_divergence.delta_paisa
    # Every stage's evidence list starts with the settlement_id (see
    # app/divergence/tracer.py's _settlement_stage/_bank_stage) — reused
    # here as the citation for rules with no more specific record to point
    # to (currency_rounding).
    settlement_id = first_divergence.evidence[0] if first_divergence.evidence else None

    if first_divergence.stage == "settlement":
        for r in group_refunds:
            if delta == r.amount_paisa:
                # settlement OVERSTATES itself by exactly this refund's
                # amount — it never netted the refund out.
                return RootCauseProposal(
                    root_cause="missing_refund_netting", claimed_adjustment_paisa=delta,
                    confidence=1.0, supporting_evidence_ids=[r.refund_id],
                )
            if delta == -r.amount_paisa:
                # settlement UNDERSTATES itself by exactly this refund's
                # amount — it was netted an extra, duplicate time.
                return RootCauseProposal(
                    root_cause="duplicate_refund", claimed_adjustment_paisa=delta,
                    confidence=1.0, supporting_evidence_ids=[r.refund_id],
                )

        if 0 < abs(delta) <= CURRENCY_ROUNDING_MAX_PAISA:
            evidence = [settlement_id] if settlement_id else []
            return RootCauseProposal(
                root_cause="currency_rounding", claimed_adjustment_paisa=delta,
                confidence=1.0, supporting_evidence_ids=evidence,
            )

    if first_divergence.stage == "bank":
        expected = first_divergence.expected_paisa
        actual = first_divergence.actual_paisa
        if len(bank_txns) == 2 and expected and actual == 2 * expected:
            return RootCauseProposal(
                root_cause="duplicate_bank_credit", claimed_adjustment_paisa=delta,
                confidence=1.0, supporting_evidence_ids=[b.bank_txn_id for b in bank_txns],
            )

    return None
