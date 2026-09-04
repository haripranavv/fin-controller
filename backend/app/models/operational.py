"""Operational/agent entities: Batch, ReconciliationCase, Match, AgentEvent,
Evidence, Investigation, ExceptionRecord, EvaluationRun.

PROJECT_SPEC.md section 5, extended in two places where a later section
imposes a stricter functional requirement than section 5's minimal field
list (see docs/ARCHITECTURE_NOTES.md for the full reasoning):

- AgentEvent: section 5 lists a single `state` field, but section 14 requires
  "case, previous state, next state, tool, timestamp, relevant input/
  reference, output, verifier result" for a real audit trail. This model
  uses from_state/to_state/verifier_result instead of a bare `state`.
- EvaluationRun: section 5's field list is missing three metrics that
  section 16 requires (match rate by value, exception value, AI-assisted
  resolution rate). Added as nullable columns so baseline runs (which don't
  compute an AI-assisted-resolution-rate) can leave it null.

Every bounded-enum column uses app.models.enums.sa_enum() rather than a bare
sqlalchemy.Enum(...) — see that function's docstring for why (it fixes two
real gaps: enums stored by member .name instead of .value, and no DB-level
CHECK constraint by default).
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)

from app.db.session import Base
from app.db.types import BIGINT_PK
from app.models.enums import (
    CaseState,
    DivergenceStage,
    EvalMode,
    MatchMethod,
    RecordType,
    RootCause,
    Severity,
    sa_enum,
)


class Batch(Base):
    __tablename__ = "batches"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    batch_id = Column(String(64), unique=True, nullable=False, index=True)
    dataset_version = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status = Column(String(32), nullable=False, default="created", server_default="created")
    # Ownership for user isolation (app/api's own concern, not the
    # orchestrator's - app.orchestrator.batch_runner.get_or_create_batch,
    # UNCHANGED, never sets this, so a batch created by a script or an
    # older run stays NULL = "system-owned, visible to every
    # authenticated user" for backward compatibility. Only
    # app.api.routes_import (which creates the row itself, before the
    # unchanged orchestrator ever touches it) sets a real owner.
    user_id = Column(BIGINT_PK, ForeignKey("users.id"), nullable=True, index=True)


class ReconciliationCase(Base):
    """The unit of work for the agent. One case is a financial investigation
    chain, usually anchored on a settlement or related financial event."""

    __tablename__ = "reconciliation_cases"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id = Column(String(64), unique=True, nullable=False, index=True)
    batch_id = Column(String(64), ForeignKey("batches.batch_id"), nullable=False, index=True)
    anchor_type = Column(sa_enum(RecordType, "case_anchor_type_enum"), nullable=False)
    # Polymorphic reference resolved via (anchor_type, anchor_id); no single
    # FK target is possible since anchor_type varies per case.
    anchor_id = Column(String(64), nullable=False, index=True)
    state = Column(
        sa_enum(CaseState, "case_state_enum"),
        nullable=False,
        default=CaseState.INGESTED,
        server_default=CaseState.INGESTED.value,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Match(Base):
    """Candidate/accepted relationship between two financial records."""

    __tablename__ = "matches"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id = Column(String(64), ForeignKey("reconciliation_cases.case_id"), nullable=False, index=True)
    source_type = Column(sa_enum(RecordType, "match_source_type_enum"), nullable=False)
    source_id = Column(String(64), nullable=False, index=True)
    target_type = Column(sa_enum(RecordType, "match_target_type_enum"), nullable=False)
    target_id = Column(String(64), nullable=False, index=True)
    method = Column(sa_enum(MatchMethod, "match_method_enum"), nullable=False)
    score = Column(Float, nullable=False)
    accepted = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AgentEvent(Base):
    """Audit/event stream for state transitions and tool usage. See module
    docstring for why this has from_state/to_state/verifier_result rather
    than the single `state` field in the section 5 table."""

    __tablename__ = "agent_events"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id = Column(String(64), ForeignKey("reconciliation_cases.case_id"), nullable=False, index=True)
    from_state = Column(sa_enum(CaseState, "agent_event_from_state_enum"), nullable=True)
    to_state = Column(sa_enum(CaseState, "agent_event_to_state_enum"), nullable=False)
    tool = Column(String(64), nullable=True)
    input_summary = Column(Text, nullable=True)
    output_summary = Column(Text, nullable=True)
    message = Column(Text, nullable=True)
    verifier_result = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Evidence(Base):
    """Evidence references used in matching/investigation."""

    __tablename__ = "evidence"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id = Column(String(64), ForeignKey("reconciliation_cases.case_id"), nullable=False, index=True)
    source_type = Column(sa_enum(RecordType, "evidence_source_type_enum"), nullable=False)
    source_id = Column(String(64), nullable=False, index=True)
    # Not a bounded enum: evidence_type varies with tool/context (e.g.
    # "narration_extraction", "divergence_calc", "match_candidate",
    # "root_cause_evidence") and new kinds may be added without a migration.
    evidence_type = Column(String(64), nullable=False)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Investigation(Base):
    """First-divergence / root-cause result for a case."""

    __tablename__ = "investigations"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id = Column(String(64), ForeignKey("reconciliation_cases.case_id"), nullable=False, index=True)
    divergence_stage = Column(sa_enum(DivergenceStage, "investigation_divergence_stage_enum"), nullable=True)
    expected_amount_paisa = Column(BigInteger, nullable=False)
    actual_amount_paisa = Column(BigInteger, nullable=False)
    delta_paisa = Column(BigInteger, nullable=False)
    root_cause = Column(sa_enum(RootCause, "investigation_root_cause_enum"), nullable=True)
    confidence = Column(Float, nullable=True)
    status = Column(String(32), nullable=False, default="traced", server_default="traced")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExceptionRecord(Base):
    """Cases requiring human attention. Named ExceptionRecord (not
    Exception) to avoid shadowing the builtin; the table is `exceptions`."""

    __tablename__ = "exceptions"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id = Column(String(64), ForeignKey("reconciliation_cases.case_id"), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    severity = Column(sa_enum(Severity, "exception_severity_enum"), nullable=False)
    amount_paisa = Column(BigInteger, nullable=False)
    status = Column(String(32), nullable=False, default="open", server_default="open")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvaluationRun(Base):
    """Benchmark results for one baseline or AI-enhanced run over a dataset
    version. See module docstring for the three columns added beyond
    section 5's field list."""

    __tablename__ = "evaluation_runs"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    dataset_version = Column(String(64), nullable=False, index=True)
    mode = Column(sa_enum(EvalMode, "evaluation_run_mode_enum"), nullable=False)
    records_processed = Column(Integer, nullable=False)
    match_rate = Column(Float, nullable=False)
    match_rate_by_value = Column(Float, nullable=True)
    precision = Column(Float, nullable=False)
    recall = Column(Float, nullable=False)
    false_match_rate = Column(Float, nullable=False)
    exception_count = Column(Integer, nullable=False)
    exception_value_paisa = Column(BigInteger, nullable=True)
    ai_assisted_resolution_rate = Column(Float, nullable=True)
    throughput = Column(Float, nullable=False)  # cases processed per second
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
