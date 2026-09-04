"""Pipeline input/output types."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.datagen.models import GenBankTransaction, GenOrder, GenPayment, GenRefund, GenSettlement
from app.divergence.types import DivergenceTrace
from app.matcher.types import MatchCandidate
from app.verifier.types import RootCauseProposal, VerificationResult


@dataclass
class CaseInputs:
    """Everything resolve_case needs for one order's case, assembled from
    the matcher's own accepted output (see assemble.py) — never from
    ground truth."""

    order: GenOrder
    payments: list[GenPayment]
    refunds: list[GenRefund]
    settlement: GenSettlement | None
    settlement_match: MatchCandidate | None
    settlement_group_payments: list[GenPayment] = field(default_factory=list)
    settlement_group_refunds: list[GenRefund] = field(default_factory=list)
    bank_txns: list[GenBankTransaction] = field(default_factory=list)
    bank_matches: list[MatchCandidate] = field(default_factory=list)


@dataclass
class CaseResult:
    order_id: str
    outcome: str  # "RESOLVED" | "ESCALATED"
    reason: str
    trace: DivergenceTrace | None = None
    root_cause_proposal: RootCauseProposal | None = None
    verification: VerificationResult | None = None
