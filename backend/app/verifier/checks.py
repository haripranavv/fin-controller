"""Individual deterministic checks (PROJECT_SPEC.md section 11's
checklist). Each returns a single CheckResult; app/verifier/verifier.py
combines the ones relevant to a given call into a VerificationResult.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from app.models.enums import RecordType, RootCause
from app.verifier.types import CheckResult, RootCauseProposal, VerificationResult

# "defined tolerance" (section 11) — the verifier's own default is strict
# (an accepted match should reconcile exactly); a caller with a legitimate
# reason for slack (e.g. a future divergence engine handling
# currency_rounding) passes tolerance_paisa explicitly. The verifier never
# picks a wider tolerance on its own.
DEFAULT_TOLERANCE_PAISA = 0

# Section 10: "< 0.60 -> escalate". Re-checked here (not just by whatever
# calls the root-cause investigator) because the verifier is "the final
# authority" (section 11) — it does not trust that an upstream gate ran.
MIN_ROOT_CAUSE_CONFIDENCE = 0.60

# The only source_type -> target_type pairs that represent a real hop in
# the financial chain (PROJECT_SPEC.md section 4/12: Order -> Payment ->
# Refund -> Settlement -> Bank). Payment(s) -> Settlement is many-to-one by
# design (section 8.4); every other pair here can be one-to-many.
ALLOWED_RELATIONSHIPS: set[tuple[str, str]] = {
    (RecordType.ORDER.value, RecordType.PAYMENT.value),
    (RecordType.PAYMENT.value, RecordType.REFUND.value),
    (RecordType.PAYMENT.value, RecordType.SETTLEMENT.value),
    (RecordType.SETTLEMENT.value, RecordType.BANK_TRANSACTION.value),
}

_ROOT_CAUSE_VALUES = {c.value for c in RootCause}


class MatchLike(Protocol):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    accepted: bool


def verify_reconciliation(
    expected_paisa: int, actual_paisa: int, *, tolerance_paisa: int = DEFAULT_TOLERANCE_PAISA, label: str = "amount"
) -> CheckResult:
    """The core arithmetic check (section 11's example: expected - claimed
    = actual). Money is always integer paisa — never compare as float."""
    delta = actual_paisa - expected_paisa
    passed = abs(delta) <= tolerance_paisa
    detail = f"expected={expected_paisa} actual={actual_paisa} delta={delta} tolerance={tolerance_paisa}"
    return CheckResult(name=f"{label}_arithmetic", passed=passed, detail=detail)


def verify_chronology(events: Sequence[tuple[str, datetime]]) -> CheckResult:
    """Each hop's timestamp must be >= the previous hop's — a chain can't
    be refunded before it was paid, settled before it was refunded, etc.
    `events` is the chain in expected order, e.g.
    [("order", ...), ("payment", ...), ("refund", ...), ...]."""
    for (prev_label, prev_ts), (label, ts) in zip(events, events[1:]):
        if ts < prev_ts:
            return CheckResult(
                name="chronology", passed=False,
                detail=f"{label} ({ts}) precedes {prev_label} ({prev_ts})",
            )
    return CheckResult(name="chronology", passed=True, detail=f"{len(events)} hop(s), all non-decreasing")


def verify_relationship(source_type: str, target_type: str) -> CheckResult:
    passed = (source_type, target_type) in ALLOWED_RELATIONSHIPS
    detail = f"{source_type} -> {target_type}"
    if not passed:
        detail += " is not a recognized financial-chain relationship"
    return CheckResult(name="relationship_consistency", passed=passed, detail=detail)


def verify_no_double_counting(accepted_matches: Sequence[MatchLike]) -> CheckResult:
    """Section 8.4: "no record can be reused across accepted matches" —
    scoped precisely to the relationships where reuse is actually illegal:

    - payment -> settlement is many-to-one: a payment belongs to exactly
      one settlement (a settlement legitimately covers many payments).
    - settlement -> bank_transaction: a settlement MAY legitimately have
      two bank txns (duplicate_bank_credit), but one bank credit must not
      fund two different settlements (the reverse is never legitimate).

    Deliberately does NOT flag order -> payment (partial_payment is a
    legitimate one-to-many) or payment -> refund (multiple partial refunds
    against one payment is legitimate).

    This is independent of app.matcher's own no-reuse bookkeeping — the
    verifier is "the final authority" (section 11) and re-checks rather
    than trusting the matcher got it right.
    """
    payment_to_settlement: dict[str, str] = {}
    for m in accepted_matches:
        if not m.accepted or m.target_type != RecordType.SETTLEMENT.value:
            continue
        prior = payment_to_settlement.get(m.source_id)
        if prior is not None and prior != m.target_id:
            return CheckResult(
                name="no_double_counting", passed=False,
                detail=f"payment {m.source_id} accepted into both settlement {prior} and {m.target_id}",
            )
        payment_to_settlement[m.source_id] = m.target_id

    bank_to_settlement: dict[str, str] = {}
    for m in accepted_matches:
        if not m.accepted or m.target_type != RecordType.BANK_TRANSACTION.value:
            continue
        prior = bank_to_settlement.get(m.target_id)
        if prior is not None and prior != m.source_id:
            return CheckResult(
                name="no_double_counting", passed=False,
                detail=f"bank txn {m.target_id} credited to both settlement {prior} and {m.source_id}",
            )
        bank_to_settlement[m.target_id] = m.source_id

    return CheckResult(name="no_double_counting", passed=True, detail=f"checked {len(accepted_matches)} accepted match(es)")


def verify_root_cause_proposal(
    proposal: RootCauseProposal,
    expected_paisa: int,
    actual_paisa: int,
    known_evidence_ids: set[str],
    *,
    tolerance_paisa: int = DEFAULT_TOLERANCE_PAISA,
) -> VerificationResult:
    """Section 11's checklist items "AI-derived match consistency" and
    "root-cause amount coverage" together: an AI-proposed root cause is
    untrusted (section 2) until it passes ALL of — a bounded cause (section
    10: "the AI cannot invent new cause categories"), a confidence gate
    (section 10; re-checked here per the module docstring), evidence that
    actually exists, and arithmetic that actually closes the gap (section
    11's worked example). Any single failure fails the whole proposal —
    "AI confidence is never enough" (section 11) even when the arithmetic
    happens to work out, and vice versa.
    """
    checks: list[CheckResult] = []

    bounded = proposal.root_cause in _ROOT_CAUSE_VALUES
    checks.append(CheckResult(
        "bounded_root_cause", bounded,
        f"{proposal.root_cause!r} {'is' if bounded else 'is NOT'} in the allowed root-cause set",
    ))

    confident = proposal.confidence >= MIN_ROOT_CAUSE_CONFIDENCE
    checks.append(CheckResult(
        "confidence_gate", confident,
        f"confidence={proposal.confidence} threshold={MIN_ROOT_CAUSE_CONFIDENCE}",
    ))

    has_evidence = bool(proposal.supporting_evidence_ids) and all(
        e in known_evidence_ids for e in proposal.supporting_evidence_ids
    )
    checks.append(CheckResult(
        "evidence_referenced", has_evidence,
        f"{len(proposal.supporting_evidence_ids)} evidence id(s) cited, all known={has_evidence}",
    ))

    predicted_actual = expected_paisa + proposal.claimed_adjustment_paisa
    residual = actual_paisa - predicted_actual
    covers = abs(residual) <= tolerance_paisa
    checks.append(CheckResult(
        "root_cause_amount_coverage", covers,
        f"expected={expected_paisa} + adjustment={proposal.claimed_adjustment_paisa} = {predicted_actual}, "
        f"actual={actual_paisa}, residual={residual}, tolerance={tolerance_paisa}",
    ))

    return VerificationResult.combine(checks)
