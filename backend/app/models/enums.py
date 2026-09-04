"""Bounded enumerations shared across models.

All enums are stored as VARCHAR + CHECK constraint (native_enum=False on the
SQLAlchemy Enum type) rather than native Postgres ENUM types. This trades a
small amount of DB-level rigor for migrations that don't need ALTER TYPE ...
ADD VALUE ceremony when the bounded set changes, and for portability with the
SQLite-based unit tests. See docs/ARCHITECTURE_NOTES.md.
"""
import enum

from sqlalchemy import Enum as SAEnum


class CaseState(str, enum.Enum):
    """PROJECT_SPEC.md section 6 — the bounded agent state machine."""

    INGESTED = "INGESTED"
    MATCH_ATTEMPT = "MATCH_ATTEMPT"
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"
    NARRATION_EXTRACT = "NARRATION_EXTRACT"
    RE_MATCH = "RE_MATCH"
    VERIFY = "VERIFY"
    DIVERGENCE_TRACE = "DIVERGENCE_TRACE"
    ROOT_CAUSE_INVESTIGATE = "ROOT_CAUSE_INVESTIGATE"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


class RecordType(str, enum.Enum):
    """The five financial entity types, used for polymorphic references
    (ReconciliationCase.anchor_type, Match.source_type/target_type,
    Evidence.source_type)."""

    ORDER = "order"
    PAYMENT = "payment"
    REFUND = "refund"
    SETTLEMENT = "settlement"
    BANK_TRANSACTION = "bank_transaction"


class DivergenceStage(str, enum.Enum):
    """PROJECT_SPEC.md section 12 — hops in the financial chain."""

    ORDER = "order"
    PAYMENT = "payment"
    REFUND = "refund"
    SETTLEMENT = "settlement"
    BANK = "bank"


class RootCause(str, enum.Enum):
    """PROJECT_SPEC.md section 10 — the closed set of allowed root causes.
    The AI investigator cannot invent new categories; anything else it
    proposes fails schema validation and the case escalates."""

    DUPLICATE_REFUND = "duplicate_refund"
    MISSING_REFUND_NETTING = "missing_refund_netting"
    UNREPORTED_FEE = "unreported_fee"
    PARTIAL_SETTLEMENT_SPLIT = "partial_settlement_split"
    CURRENCY_ROUNDING = "currency_rounding"
    DUPLICATE_BANK_CREDIT = "duplicate_bank_credit"
    UNMATCHED_EXTERNAL_DEDUCTION = "unmatched_external_deduction"
    UNKNOWN = "unknown"


class MatchMethod(str, enum.Enum):
    """How a Match row was produced. NARRATION_AI_ASSISTED means AI-extracted
    fields fed the re-match — the match itself is still made by deterministic
    logic, never chosen by the AI (PROJECT_SPEC.md section 2)."""

    EXACT_REFERENCE = "exact_reference"
    FUZZY_CANDIDATE = "fuzzy_candidate"
    SUBSET_SUM_BATCH = "subset_sum_batch"
    NARRATION_AI_ASSISTED = "narration_ai_assisted"
    MANUAL = "manual"


class Severity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvalMode(str, enum.Enum):
    """PROJECT_SPEC.md section 16."""

    BASELINE = "baseline"
    AI_ENHANCED = "ai_enhanced"


def sa_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Build a SQLAlchemy Enum column type that stores/validates against the
    Python enum's `.value` (e.g. "settlement"), not its member `.name` (e.g.
    "SETTLEMENT") — SQLAlchemy's Enum type uses `.name` by default, which
    would silently mismatch every lowercase value PROJECT_SPEC.md's JSON
    contracts use (root causes, record types, etc.) for every enum here
    except CaseState, where name happens to equal value. Centralized here so
    that mistake can't be reintroduced column-by-column.

    Also always creates a DB-level CHECK constraint (create_constraint=True
    — SQLAlchemy's default is False as of 1.4+) so the bounded set is
    enforced by Postgres, not just by the ORM.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        create_constraint=True,
        values_callable=lambda obj: [e.value for e in obj],
    )
