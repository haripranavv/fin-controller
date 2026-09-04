"""Internal (pre-persistence) representation of a generated dataset.

Plain dataclasses, not the SQLAlchemy models in app.models — keeps
generation logic testable without a database, and keeps ground truth
(GenGroundTruth) physically separate from the financial records in the
generation logic itself, mirroring the isolation the real app enforces (see
app/db/groundtruth_session.py and this package's __init__.py).

Field names on GenOrder/GenPayment/GenRefund/GenSettlement/
GenBankTransaction deliberately match the corresponding SQLAlchemy model's
column names 1:1 (see app/models/financial.py) so app.datagen.persist can
construct ORM objects with **dataclass.__dict__ instead of a hand-written
field mapping that could drift out of sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# --- axis A: per-flow "how hard is this to match" category -----------------
AXIS_A_CLEAN = "clean"
AXIS_A_MESSY_NARRATION = "messy_narration"
AXIS_A_DUPLICATE_REFERENCE = "duplicate_reference"
AXIS_A_DELAYED_EVENT = "delayed_event"
AXIS_A_PARTIAL_PAYMENT = "partial_payment"
AXIS_A_REFUND_PARTIAL = "refund_partial"
AXIS_A_REFUND_FULL = "refund_full"

# --- axis B: per-settlement-group divergence scenario -----------------------
# Values match app.models.enums.RootCause where the scenario IS a root
# cause. AXIS_B_NONE and AXIS_B_PARTIAL_SETTLEMENT_SPLIT are not part of the
# per-group weighted pick in settlement.py (NONE is just "nothing chosen";
# PARTIAL_SETTLEMENT_SPLIT is applied out-of-band to a reserved slice of
# flows before grouping even happens) but live here for a single source of
# truth on the string values.
AXIS_B_NONE = "none"
AXIS_B_UNREPORTED_FEE = "unreported_fee"
AXIS_B_MISSING_REFUND_NETTING = "missing_refund_netting"
AXIS_B_DUPLICATE_REFUND = "duplicate_refund"
AXIS_B_CURRENCY_ROUNDING = "currency_rounding"
AXIS_B_DUPLICATE_BANK_CREDIT = "duplicate_bank_credit"
AXIS_B_UNMATCHED_EXTERNAL_DEDUCTION = "unmatched_external_deduction"
AXIS_B_UNRESOLVABLE_MISSING_BANK = "unresolvable_missing_bank"
AXIS_B_AMBIGUOUS_CAUSE = "ambiguous_cause"
AXIS_B_PARTIAL_SETTLEMENT_SPLIT = "partial_settlement_split"


@dataclass
class GenOrder:
    order_id: str
    merchant_id: str
    amount_paisa: int
    currency: str
    status: str
    created_at: datetime


@dataclass
class GenPayment:
    payment_id: str
    order_id: str
    amount_paisa: int
    fee_paisa: int
    tax_on_fee_paisa: int
    method: str
    status: str
    narration: str | None
    created_at: datetime


@dataclass
class GenRefund:
    refund_id: str
    payment_id: str
    amount_paisa: int
    reason_code: str | None
    narration: str | None
    created_at: datetime


@dataclass
class GenSettlement:
    settlement_id: str
    merchant_id: str
    settled_amount_paisa: int
    fee_deducted_paisa: int
    period_start: datetime
    period_end: datetime
    created_at: datetime


@dataclass
class GenBankTransaction:
    bank_txn_id: str
    amount_paisa: int
    value_date: datetime
    utr_ref: str | None
    narration: str | None


@dataclass
class GenGroundTruth:
    record_id: str  # == the flow's order_id
    true_match_ids: list[str]
    true_divergence_stage: str | None
    true_root_cause: str | None
    is_ambiguous: bool
    injected_noise_type: str


@dataclass
class OrderFlow:
    """One order flow: everything hanging directly off one order, before
    settlement grouping. Mutable (refunds can be injected later by
    settlement.py's divergence scenarios) — not a frozen dataclass."""

    order: GenOrder
    payments: list[GenPayment]
    refunds: list[GenRefund]
    axis_a_category: str
    merchant_name: str
    flow_idx: int

    @property
    def net_contribution_paisa(self) -> int:
        """What this flow should contribute to whatever settlement it ends
        up batched into: sum(payment.amount - fee - tax_on_fee) -
        sum(refund.amount) — PROJECT_SPEC.md section 12's "Settlement
        expected" formula, computed per-flow before summing across a group.
        A live property (not cached) so refund injection updates it for free.
        """
        gross = sum(p.amount_paisa - p.fee_paisa - p.tax_on_fee_paisa for p in self.payments)
        refunded = sum(r.amount_paisa for r in self.refunds)
        return gross - refunded

    @property
    def match_ids(self) -> list[str]:
        return [p.payment_id for p in self.payments] + [r.refund_id for r in self.refunds]


@dataclass
class GeneratedBatch:
    batch_id: str
    dataset_version: str
    seed: int
    orders: list[GenOrder] = field(default_factory=list)
    payments: list[GenPayment] = field(default_factory=list)
    refunds: list[GenRefund] = field(default_factory=list)
    settlements: list[GenSettlement] = field(default_factory=list)
    bank_transactions: list[GenBankTransaction] = field(default_factory=list)
    ground_truth: list[GenGroundTruth] = field(default_factory=list)


def order_id(dataset_version: str, idx: int) -> str:
    return f"ord_{dataset_version}_{idx:05d}"


def payment_id(dataset_version: str, idx: int, suffix: str = "") -> str:
    return f"pay_{dataset_version}_{idx:05d}{suffix}"


def refund_id(dataset_version: str, idx: int, suffix: str = "") -> str:
    return f"rfd_{dataset_version}_{idx:05d}{suffix}"


def settlement_id(dataset_version: str, idx: int, suffix: str = "") -> str:
    return f"stl_{dataset_version}_{idx:05d}{suffix}"


def bank_txn_id(dataset_version: str, idx: int, suffix: str = "") -> str:
    return f"bnk_{dataset_version}_{idx:05d}{suffix}"


def batch_id(dataset_version: str) -> str:
    return f"batch_{dataset_version}"
