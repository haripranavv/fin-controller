"""The deterministic_matcher tool (PROJECT_SPEC.md section 7), implementing
section 8's stages across the financial chain Order -> Payment -> Refund ->
Settlement -> Bank Transaction.

Order<->Payment and Payment<->Refund are direct foreign keys in this schema
(PROJECT_SPEC.md section 4) — matching them is an exact-reference lookup,
not a search problem. The real matching problem section 8 is concerned with
is the two legs with NO foreign key by design: Payment(s)<->Settlement
(bounded subset-sum, section 8.4) and Settlement<->Bank Transaction
(reference extraction with a fuzzy fallback, sections 8.2/8.3).

Narration/name similarity (one of section 8.3's four candidate-matching
signals) is used for the settlement<->bank leg, where narration is the only
place a strong reference lives. It is NOT used for payment<->settlement
matching: Settlement has no narration field to compare against, and the
messy/unseen narration on Payment records (see app/datagen/catalog.py) is
reserved for the AI narration_extractor tool (section 9), invoked later by
the orchestrator when this matcher's settlement-matching stage reports
"ambiguous" or "no_match" — not consumed here. See docs/ARCHITECTURE_NOTES.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.matcher import normalize, subset_sum
from app.matcher.scoring import (
    AMBIGUITY_MARGIN_PAISA,
    BANK_FALLBACK_ACCEPT_THRESHOLD,
    SETTLEMENT_MATCH_ACCEPT_THRESHOLD,
    amount_score,
    date_proximity_score,
)
from app.matcher.types import MatchCandidate, MatchRunReport
from app.models.enums import MatchMethod, RecordType

# A true settlement member's payment.created_at is guaranteed to fall
# within [period_start, period_end] by construction (the generator derives
# period_end from max(payment.created_at) across the group — see
# app/datagen/settlement.py). A wide slack here doesn't help find true
# members; it only lets a payment bleed into an ADJACENT settlement's
# candidate pool and get greedily consumed there before its true settlement
# is processed. Found via test_clean_settlements_are_recovered_exactly —
# see docs/ARCHITECTURE_NOTES.md. Kept small and non-zero purely as a
# boundary-rounding safety margin, not a real search-widening tool.
SETTLEMENT_DATE_WINDOW_SLACK_DAYS = 0.5
BANK_DATE_WINDOW_SLACK_DAYS = 5.0


# --- Order <-> Payment, Payment <-> Refund: exact FK lookups ----------------


def match_orders_payments(orders: list[GenOrder], payments: list[GenPayment]) -> list[MatchCandidate]:
    order_ids = {o.order_id for o in orders}
    return [
        MatchCandidate(
            source_type=RecordType.ORDER.value, source_id=p.order_id,
            target_type=RecordType.PAYMENT.value, target_id=p.payment_id,
            method=MatchMethod.EXACT_REFERENCE.value, score=1.0, accepted=True,
        )
        for p in payments
        if p.order_id in order_ids
    ]


def match_payments_refunds(payments: list[GenPayment], refunds: list[GenRefund]) -> list[MatchCandidate]:
    payment_ids = {p.payment_id for p in payments}
    return [
        MatchCandidate(
            source_type=RecordType.PAYMENT.value, source_id=r.payment_id,
            target_type=RecordType.REFUND.value, target_id=r.refund_id,
            method=MatchMethod.EXACT_REFERENCE.value, score=1.0, accepted=True,
        )
        for r in refunds
        if r.payment_id in payment_ids
    ]


# --- Payment(s) <-> Settlement: bounded subset-sum --------------------------


@dataclass
class _PaymentContribution:
    payment_id: str
    merchant_id: str
    net_contribution_paisa: int
    created_at: datetime


def compute_net_contributions(
    payments: list[GenPayment], refunds: list[GenRefund], orders: list[GenOrder]
) -> list[_PaymentContribution]:
    """Per-payment net contribution = amount - fee - tax_on_fee -
    sum(its refunds) — PROJECT_SPEC.md section 12's "Settlement expected"
    formula, computed per payment before subset-sum sums it across a group.
    """
    merchant_by_order = {o.order_id: o.merchant_id for o in orders}
    refunded_by_payment: dict[str, int] = {}
    for r in refunds:
        refunded_by_payment[r.payment_id] = refunded_by_payment.get(r.payment_id, 0) + r.amount_paisa

    return [
        _PaymentContribution(
            payment_id=p.payment_id,
            merchant_id=merchant_by_order.get(p.order_id, ""),
            net_contribution_paisa=p.amount_paisa - p.fee_paisa - p.tax_on_fee_paisa - refunded_by_payment.get(p.payment_id, 0),
            created_at=p.created_at,
        )
        for p in payments
    ]


def match_settlement_payments(
    settlements: list[GenSettlement], contributions: list[_PaymentContribution]
) -> tuple[list[MatchCandidate], list[MatchRunReport]]:
    """Process settlements oldest-first; a payment accepted into one
    settlement is removed from the candidate pool for later ones (section
    8.4: "no record can be reused across accepted matches")."""
    candidates_out: list[MatchCandidate] = []
    reports: list[MatchRunReport] = []
    consumed: set[str] = set()

    for s in sorted(settlements, key=lambda s: s.created_at):
        pool = [
            c
            for c in contributions
            if c.merchant_id == s.merchant_id
            and c.payment_id not in consumed
            and date_proximity_score(c.created_at, s.period_start, s.period_end, decay_days=SETTLEMENT_DATE_WINDOW_SLACK_DAYS) > 0
        ]

        if not pool:
            reports.append(MatchRunReport("settlement", s.settlement_id, "no_match", "no candidate payments in date/merchant window"))
            continue

        if len(pool) > subset_sum.MAX_ITEMS:
            reports.append(MatchRunReport("settlement", s.settlement_id, "too_many_candidates", f"{len(pool)} candidates"))
            continue

        items = [(c.payment_id, c.net_contribution_paisa) for c in pool]
        results = subset_sum.closest_subset_sums(items, s.settled_amount_paisa, k=3)
        best = results[0]

        if not best.member_ids:
            reports.append(MatchRunReport("settlement", s.settlement_id, "no_match", "closest subset is empty"))
            continue

        score = amount_score(best.delta, s.settled_amount_paisa)
        ambiguous = len(results) >= 2 and abs(abs(results[1].delta) - abs(best.delta)) <= AMBIGUITY_MARGIN_PAISA
        accepted = score >= SETTLEMENT_MATCH_ACCEPT_THRESHOLD and not ambiguous

        for pid in best.member_ids:
            candidates_out.append(
                MatchCandidate(
                    source_type=RecordType.PAYMENT.value, source_id=pid,
                    target_type=RecordType.SETTLEMENT.value, target_id=s.settlement_id,
                    method=MatchMethod.SUBSET_SUM_BATCH.value, score=round(score, 4), accepted=accepted,
                )
            )
            if accepted:
                consumed.add(pid)

        if ambiguous:
            reports.append(MatchRunReport("settlement", s.settlement_id, "ambiguous", f"top-2 candidates tie within {AMBIGUITY_MARGIN_PAISA}p (delta={best.delta})"))
        elif accepted:
            reports.append(MatchRunReport("settlement", s.settlement_id, "matched", f"score={score:.2f} delta={best.delta} members={len(best.member_ids)}"))
        else:
            reports.append(MatchRunReport("settlement", s.settlement_id, "no_match", f"best score {score:.2f} below threshold {SETTLEMENT_MATCH_ACCEPT_THRESHOLD}"))

    return candidates_out, reports


# --- Settlement <-> Bank Transaction: reference then fallback --------------


def match_settlement_bank(
    settlements: list[GenSettlement], bank_txns: list[GenBankTransaction]
) -> tuple[list[MatchCandidate], list[MatchRunReport]]:
    candidates_out: list[MatchCandidate] = []
    reports: list[MatchRunReport] = []

    # Pass 1: exact reference, for every settlement. Every bank txn claimed
    # this way is removed from the fallback pool below — otherwise the
    # amount+date fallback for a DIFFERENT (e.g. genuinely bank-less)
    # settlement can accidentally "steal" a bank txn that truly belongs to
    # some other settlement just because its amount/date happen to be
    # close. Found via test_unresolvable_missing_bank_yields_no_bank_match.
    unmatched: list[GenSettlement] = []
    referenced_bank_ids: set[str] = set()
    for s in settlements:
        reference_hits = [b for b in bank_txns if normalize.contains_reference(b.narration, s.settlement_id)]
        if reference_hits:
            for b in reference_hits:
                candidates_out.append(
                    MatchCandidate(
                        source_type=RecordType.SETTLEMENT.value, source_id=s.settlement_id,
                        target_type=RecordType.BANK_TRANSACTION.value, target_id=b.bank_txn_id,
                        method=MatchMethod.EXACT_REFERENCE.value, score=1.0, accepted=True,
                    )
                )
                referenced_bank_ids.add(b.bank_txn_id)
            reports.append(MatchRunReport("settlement", s.settlement_id, "matched", f"{len(reference_hits)} bank txn(s) by reference"))
        else:
            unmatched.append(s)

    # Pass 2: amount+date fallback, only for settlements with no reference
    # hit, only over bank txns nothing already claimed by reference.
    fallback_pool = [b for b in bank_txns if b.bank_txn_id not in referenced_bank_ids]
    for s in unmatched:
        window_start = s.created_at
        window_end = s.created_at + timedelta(days=BANK_DATE_WINDOW_SLACK_DAYS)
        best_txn = None
        best_score = 0.0
        for b in fallback_pool:
            a_score = amount_score(b.amount_paisa - s.settled_amount_paisa, s.settled_amount_paisa)
            d_score = date_proximity_score(b.value_date, window_start, window_end, decay_days=BANK_DATE_WINDOW_SLACK_DAYS)
            combined = 0.7 * a_score + 0.3 * d_score
            if combined > best_score:
                best_score, best_txn = combined, b

        if best_txn is not None and best_score >= BANK_FALLBACK_ACCEPT_THRESHOLD:
            candidates_out.append(
                MatchCandidate(
                    source_type=RecordType.SETTLEMENT.value, source_id=s.settlement_id,
                    target_type=RecordType.BANK_TRANSACTION.value, target_id=best_txn.bank_txn_id,
                    method=MatchMethod.FUZZY_CANDIDATE.value, score=round(best_score, 4), accepted=True,
                )
            )
            reports.append(MatchRunReport("settlement", s.settlement_id, "matched", f"fallback score={best_score:.2f}"))
        else:
            reports.append(MatchRunReport("settlement", s.settlement_id, "no_match", "no reference and no strong fallback candidate"))

    return candidates_out, reports


# --- top-level orchestration -------------------------------------------------


@dataclass
class MatcherRunResult:
    order_payment: list[MatchCandidate]
    payment_refund: list[MatchCandidate]
    settlement_payment: list[MatchCandidate]
    settlement_bank: list[MatchCandidate]
    settlement_payment_reports: list[MatchRunReport]
    settlement_bank_reports: list[MatchRunReport]

    @property
    def all_matches(self) -> list[MatchCandidate]:
        return self.order_payment + self.payment_refund + self.settlement_payment + self.settlement_bank


def run_deterministic_matching(
    orders: list[GenOrder],
    payments: list[GenPayment],
    refunds: list[GenRefund],
    settlements: list[GenSettlement],
    bank_txns: list[GenBankTransaction],
) -> MatcherRunResult:
    order_payment = match_orders_payments(orders, payments)
    payment_refund = match_payments_refunds(payments, refunds)
    contributions = compute_net_contributions(payments, refunds, orders)
    settlement_payment, sp_reports = match_settlement_payments(settlements, contributions)
    settlement_bank, sb_reports = match_settlement_bank(settlements, bank_txns)
    return MatcherRunResult(
        order_payment=order_payment,
        payment_refund=payment_refund,
        settlement_payment=settlement_payment,
        settlement_bank=settlement_bank,
        settlement_payment_reports=sp_reports,
        settlement_bank_reports=sb_reports,
    )
