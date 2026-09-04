"""Deterministic re-match after narration extraction (PROJECT_SPEC.md
section 6: NARRATION_EXTRACT -> confidence gate -> high -> RE_MATCH).

Reuses app.matcher's OWN scoring/threshold/subset-sum machinery UNCHANGED
(app.matcher.scoring.SETTLEMENT_MATCH_ACCEPT_THRESHOLD, app.matcher.subset_sum,
app.matcher.reconciler.compute_net_contributions,
app.matcher.reconciler.SETTLEMENT_DATE_WINDOW_SLACK_DAYS — all imported, not
redefined, so there is no drift-prone duplicate).

What narration extraction actually contributes: the ONE payment it was
run on is allowed to bypass the settlement date-window filter that
app.matcher.reconciler.match_settlement_payments applied on its first
pass — nothing else about the candidate pool changes, and every other
candidate in that pool is still date-filtered exactly as before. The
accept/reject decision is made by the IDENTICAL deterministic formula and
threshold as any other settlement match (section 9: "AI confidence alone
never creates a match" — it only justifies searching one payment's way
into a settlement's candidate pool; the same math decides whether it
actually belongs there).
"""
from __future__ import annotations

from app.datagen.models import GenOrder, GenPayment, GenRefund, GenSettlement
from app.matcher import subset_sum
from app.matcher.reconciler import SETTLEMENT_DATE_WINDOW_SLACK_DAYS, compute_net_contributions
from app.matcher.scoring import SETTLEMENT_MATCH_ACCEPT_THRESHOLD, amount_score, date_proximity_score
from app.matcher.types import MatchCandidate
from app.models.enums import MatchMethod, RecordType
from app.narration.types import NarrationExtraction

# How far an extraction's amount_hint may differ from the payment's own
# recorded amount before this extraction is untrusted for widening the
# search at all (section 2: AI output stays untrusted until checked —
# applied here as an input-sanity guard, before any matching is attempted).
AMOUNT_HINT_TOLERANCE_FRACTION = 0.20


def attempt_rematch(
    target_payment_id: str,
    extraction: NarrationExtraction,
    orders: list[GenOrder],
    payments: list[GenPayment],
    refunds: list[GenRefund],
    candidate_settlements: list[GenSettlement],
    already_consumed_payment_ids: set[str],
) -> MatchCandidate | None:
    """Try to place `target_payment_id` into one of `candidate_settlements`
    (normally: every settlement for that payment's merchant), given a
    confidence-gated NarrationExtraction. Returns an accepted
    MatchCandidate (method=NARRATION_AI_ASSISTED) or None."""
    contributions = compute_net_contributions(payments, refunds, orders)
    contributions_by_id = {c.payment_id: c for c in contributions}
    target = contributions_by_id.get(target_payment_id)
    if target is None:
        return None

    if extraction.amount_hint is not None:
        payment_amount = next((p.amount_paisa for p in payments if p.payment_id == target_payment_id), None)
        if payment_amount is not None:
            if abs(extraction.amount_hint - payment_amount) > payment_amount * AMOUNT_HINT_TOLERANCE_FRACTION:
                return None  # the AI's own hint contradicts the recorded amount — don't trust it to widen anything

    best_candidate: MatchCandidate | None = None
    best_score = 0.0

    for settlement in candidate_settlements:
        if settlement.merchant_id != target.merchant_id:
            continue

        pool = [
            c
            for c in contributions
            if c.merchant_id == settlement.merchant_id
            and c.payment_id not in already_consumed_payment_ids
            and c.payment_id != target_payment_id
            and date_proximity_score(
                c.created_at, settlement.period_start, settlement.period_end, decay_days=SETTLEMENT_DATE_WINDOW_SLACK_DAYS
            )
            > 0
        ]
        pool.append(target)  # the narration-confirmed payment alone bypasses the date filter
        if len(pool) > subset_sum.MAX_ITEMS:
            continue  # still bounded — section 8.4

        items = [(c.payment_id, c.net_contribution_paisa) for c in pool]
        best = subset_sum.closest_subset_sums(items, settlement.settled_amount_paisa, k=1)[0]
        if target_payment_id not in best.member_ids:
            continue  # this settlement's best fit doesn't even need our payment

        score = amount_score(best.delta, settlement.settled_amount_paisa)
        if score >= SETTLEMENT_MATCH_ACCEPT_THRESHOLD and score > best_score:
            best_score = score
            best_candidate = MatchCandidate(
                source_type=RecordType.PAYMENT.value,
                source_id=target_payment_id,
                target_type=RecordType.SETTLEMENT.value,
                target_id=settlement.settlement_id,
                method=MatchMethod.NARRATION_AI_ASSISTED.value,
                score=round(score, 4),
                accepted=True,
            )

    return best_candidate
