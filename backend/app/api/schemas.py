"""Pydantic response models for the operator console API. Read-only DTOs —
no request bodies here beyond RunRequest (which just names a
dataset_version to hand to the unchanged orchestrator)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class BatchSummary(BaseModel):
    batch_id: str
    dataset_version: str
    status: str
    created_at: datetime
    total_cases: int
    resolved: int
    escalated: int
    in_progress: int


class EvaluationSummary(BaseModel):
    mode: str
    records_processed: int
    match_rate: float
    match_rate_by_value: float | None
    precision: float
    recall: float
    false_match_rate: float
    exception_count: int
    exception_value_paisa: int | None
    ai_assisted_resolution_rate: float | None
    throughput: float
    created_at: datetime


class EscalationReasonCount(BaseModel):
    reason_category: str
    count: int
    value_paisa: int


class AttentionCase(BaseModel):
    case_id: str
    order_id: str
    amount_paisa: int
    severity: str
    reason_category: str


class OverviewResponse(BaseModel):
    batch_id: str
    dataset_version: str
    total_cases: int
    resolved: int
    escalated: int
    in_progress: int
    resolution_rate: float
    monetary_resolution_rate: float
    total_value_paisa: int
    resolved_value_paisa: int
    exception_count: int
    exception_value_paisa: int
    throughput_cases_per_sec: float | None
    clean_resolved: int  # chain reconciled exactly, no investigation needed
    deterministic_resolved: int  # resolved via a known-cause rule
    ai_resolved: int  # resolved via the AI root-cause investigator
    top_escalation_reasons: list[EscalationReasonCount]
    attention_cases: list[AttentionCase]  # escalated, highest value first - "what needs attention"
    highlighted_case_id: str | None  # one-click suggested investigation - the top attention case, if any
    recent_events: list[AgentEventItem]  # most recent activity across the batch
    last_evaluation: list[EvaluationSummary]  # baseline + ai_enhanced, most recent per mode


class CaseListItem(BaseModel):
    case_id: str
    order_id: str
    state: str
    outcome: str  # "RESOLVED" | "ESCALATED" | "IN_PROGRESS"
    amount_paisa: int
    root_cause: str | None
    resolved_via: str | None  # "clean" | "deterministic" | "ai" | None
    severity: str | None
    finding: str  # one-line, human-readable summary of what happened to this case
    created_at: datetime
    updated_at: datetime


class CaseListResponse(BaseModel):
    total: int
    cases: list[CaseListItem]


class AgentEventItem(BaseModel):
    id: int
    case_id: str
    from_state: str | None
    to_state: str
    tool: str | None
    input_summary: str | None
    output_summary: str | None
    message: str | None
    verifier_result: dict | None
    created_at: datetime


class MatchItem(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    method: str
    score: float
    accepted: bool


class StageDTO(BaseModel):
    stage: str
    expected_paisa: int | None
    actual_paisa: int | None
    delta_paisa: int | None
    consistent: bool
    note: str
    evidence: list[str]
    is_first_divergence: bool
    timestamp: datetime | None = None  # best-available timestamp for this stage's own record(s)


class InvestigationRow(BaseModel):
    divergence_stage: str | None
    expected_amount_paisa: int
    actual_amount_paisa: int
    delta_paisa: int
    root_cause: str | None
    confidence: float | None
    status: str
    created_at: datetime


class ExceptionRow(BaseModel):
    reason: str
    severity: str
    amount_paisa: int
    status: str
    created_at: datetime


class FinancialRecord(BaseModel):
    record_type: str
    record_id: str
    amount_paisa: int
    detail: dict


class CaseDetail(BaseModel):
    case_id: str
    order_id: str
    batch_id: str
    state: str
    outcome: str
    created_at: datetime
    updated_at: datetime
    order: FinancialRecord | None
    payments: list[FinancialRecord]
    refunds: list[FinancialRecord]
    settlement: FinancialRecord | None
    bank_txns: list[FinancialRecord]
    matches: list[MatchItem]
    investigation: InvestigationRow | None
    exception: ExceptionRow | None
    events: list[AgentEventItem]


class PrivacyBoundary(BaseModel):
    """What was actually sent to Gemini for this case's root-cause
    investigation - see app/api/routes_cases.py: evidence_sent is the
    literal payload (app.rootcause.evidence.build_evidence, unchanged),
    not a description of it."""
    evidence_sent: list[dict]
    raw_files_sent: bool
    ground_truth_sent: bool
    unnecessary_pii_sent: bool
    structured_evidence_sent: bool


class InvestigationDetail(BaseModel):
    case_id: str
    order_id: str
    state: str
    outcome: str
    amount_paisa: int
    chain_available: bool
    chain: list[StageDTO]
    first_divergence_stage: str | None
    trace_status: str | None  # "clean" | "diverged" | "unresolved" | None (no settlement matched)
    downstream_impact: list[StageDTO]  # stages after the first divergence also thrown off by it
    investigation: InvestigationRow | None
    resolved_via: str | None  # "clean" | "deterministic" | "ai" | None
    ai_was_invoked: bool
    privacy: PrivacyBoundary | None
    events: list[AgentEventItem]


class ExceptionListItem(BaseModel):
    case_id: str
    order_id: str
    amount_paisa: int
    severity: str
    reason: str
    stage_reached: str  # "no_match" | "divergence_trace" | "root_cause_investigate" | "verify"
    divergence_stage: str | None
    expected_amount_paisa: int | None
    actual_amount_paisa: int | None
    delta_paisa: int | None
    status: str
    root_cause: str | None
    confidence: float | None
    verifier_result: dict | None
    created_at: datetime


class ExceptionListResponse(BaseModel):
    total: int
    total_value_paisa: int
    exceptions: list[ExceptionListItem]


class RunRequest(BaseModel):
    dataset_version: str


class RunStatus(BaseModel):
    batch_id: str
    dataset_version: str
    running: bool
    total: int | None = None
    resolved: int | None = None
    escalated: int | None = None
    errors: int | None = None
    error_message: str | None = None
