// Mirrors backend/app/api/schemas.py (+ routes_auth.py / routes_import.py's
// own response models) 1:1. Kept as plain interfaces (no runtime validation
// library) - this console only ever displays what the API returns, it never
// constructs or mutates financial state.

export interface BatchSummary {
  batch_id: string;
  dataset_version: string;
  status: string;
  created_at: string;
  total_cases: number;
  resolved: number;
  escalated: number;
  in_progress: number;
}

export interface EvaluationSummary {
  mode: string;
  records_processed: number;
  match_rate: number;
  match_rate_by_value: number | null;
  precision: number;
  recall: number;
  false_match_rate: number;
  exception_count: number;
  exception_value_paisa: number | null;
  ai_assisted_resolution_rate: number | null;
  throughput: number;
  created_at: string;
}

export interface EscalationReasonCount {
  reason_category: string;
  count: number;
  value_paisa: number;
}

export interface AttentionCase {
  case_id: string;
  order_id: string;
  amount_paisa: number;
  severity: string;
  reason_category: string;
}

export interface OverviewResponse {
  batch_id: string;
  dataset_version: string;
  total_cases: number;
  resolved: number;
  escalated: number;
  in_progress: number;
  resolution_rate: number;
  monetary_resolution_rate: number;
  total_value_paisa: number;
  resolved_value_paisa: number;
  exception_count: number;
  exception_value_paisa: number;
  throughput_cases_per_sec: number | null;
  clean_resolved: number;
  deterministic_resolved: number;
  ai_resolved: number;
  top_escalation_reasons: EscalationReasonCount[];
  attention_cases: AttentionCase[];
  highlighted_case_id: string | null;
  recent_events: AgentEventItem[];
  last_evaluation: EvaluationSummary[];
}

export interface CaseListItem {
  case_id: string;
  order_id: string;
  state: string;
  outcome: "RESOLVED" | "ESCALATED" | "IN_PROGRESS";
  amount_paisa: number;
  root_cause: string | null;
  resolved_via: "clean" | "deterministic" | "ai" | null;
  severity: string | null;
  finding: string;
  created_at: string;
  updated_at: string;
}

export interface CaseListResponse {
  total: number;
  cases: CaseListItem[];
}

export interface AgentEventItem {
  id: number;
  case_id: string;
  from_state: string | null;
  to_state: string;
  tool: string | null;
  input_summary: string | null;
  output_summary: string | null;
  message: string | null;
  verifier_result: VerifierResult | null;
  created_at: string;
}

export interface VerifierCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface VerifierResult {
  passed: boolean;
  checks: VerifierCheck[];
}

export interface MatchItem {
  source_type: string;
  source_id: string;
  target_type: string;
  target_id: string;
  method: string;
  score: number;
  accepted: boolean;
}

export interface StageDTO {
  stage: string;
  expected_paisa: number | null;
  actual_paisa: number | null;
  delta_paisa: number | null;
  consistent: boolean;
  note: string;
  evidence: string[];
  is_first_divergence: boolean;
  timestamp: string | null;
}

export interface InvestigationRow {
  divergence_stage: string | null;
  expected_amount_paisa: number;
  actual_amount_paisa: number;
  delta_paisa: number;
  root_cause: string | null;
  confidence: number | null;
  status: string;
  created_at: string;
}

export interface ExceptionRow {
  reason: string;
  severity: string;
  amount_paisa: number;
  status: string;
  created_at: string;
}

export interface FinancialRecord {
  record_type: string;
  record_id: string;
  amount_paisa: number;
  detail: Record<string, unknown>;
}

export interface CaseDetail {
  case_id: string;
  order_id: string;
  batch_id: string;
  state: string;
  outcome: string;
  created_at: string;
  updated_at: string;
  order: FinancialRecord | null;
  payments: FinancialRecord[];
  refunds: FinancialRecord[];
  settlement: FinancialRecord | null;
  bank_txns: FinancialRecord[];
  matches: MatchItem[];
  investigation: InvestigationRow | null;
  exception: ExceptionRow | null;
  events: AgentEventItem[];
}

export interface PrivacyBoundary {
  evidence_sent: Record<string, unknown>[];
  raw_files_sent: boolean;
  ground_truth_sent: boolean;
  unnecessary_pii_sent: boolean;
  structured_evidence_sent: boolean;
}

export interface InvestigationDetail {
  case_id: string;
  order_id: string;
  state: string;
  outcome: string;
  amount_paisa: number;
  chain_available: boolean;
  chain: StageDTO[];
  first_divergence_stage: string | null;
  trace_status: "clean" | "diverged" | "unresolved" | null;
  downstream_impact: StageDTO[];
  investigation: InvestigationRow | null;
  resolved_via: "clean" | "deterministic" | "ai" | null;
  ai_was_invoked: boolean;
  privacy: PrivacyBoundary | null;
  events: AgentEventItem[];
}

export interface ExceptionListItem {
  case_id: string;
  order_id: string;
  amount_paisa: number;
  severity: string;
  reason: string;
  stage_reached: string;
  divergence_stage: string | null;
  expected_amount_paisa: number | null;
  actual_amount_paisa: number | null;
  delta_paisa: number | null;
  status: string;
  root_cause: string | null;
  confidence: number | null;
  verifier_result: VerifierResult | null;
  created_at: string;
}

export interface ExceptionListResponse {
  total: number;
  total_value_paisa: number;
  exceptions: ExceptionListItem[];
}

// --- auth ---------------------------------------------------------------

export interface AuthResponse {
  token: string;
  email: string;
  display_name: string | null;
  is_demo: boolean;
}

export interface SessionResponse {
  valid: boolean;
  email: string | null;
  is_demo: boolean;
}

// --- import (server-side async job) --------------------------------------

export type ImportSourceType = "order" | "payment" | "refund" | "settlement" | "bank_transaction" | "unknown" | "rejected_ground_truth";

export interface FileDetectionResult {
  filename: string;
  detected_type: ImportSourceType;
  columns_found: string[];
  row_count: number;
  valid_row_count: number;
  invalid_row_count: number;
  duplicate_count: number;
  missing_field_count: number;
  missing_required_columns: string[];
  sample_errors: string[];
  preview_rows: Record<string, string>[];
  ready: boolean;
}

export type ImportJobStatus = "QUEUED" | "VALIDATING" | "IMPORTING" | "READY" | "FAILED";

export interface ImportJobResponse {
  job_id: string;
  status: ImportJobStatus;
  dataset_version: string | null;
  batch_id: string | null;
  error_message: string | null;
  files_total: number;
  rows_total: number;
  rows_inserted: number;
  any_ready: boolean;
  files: FileDetectionResult[];
  created_at: string;
  updated_at: string;
  // Real, incrementally-updated progress - see backend/app/api/routes_import.py's
  // _advance_stage. current_stage is null outside IMPORTING.
  current_stage: string | null;
  elapsed_seconds: number;
}

export type RunStage = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";

export interface RunStatus {
  batch_id: string;
  dataset_version: string;
  running: boolean;
  total: number | null;
  resolved: number | null;
  escalated: number | null;
  errors: number | null;
  error_message: string | null;
  stage: RunStage;
  // Live count of ReconciliationCase rows committed for this batch so
  // far - a real measurement, not a fabricated percentage. resolved/
  // escalated/errors above only change once the whole run completes.
  processed: number | null;
  elapsed_seconds: number | null;
}
