// Translates the real, unchanged backend state machine (CaseState
// transitions, tool names, root-cause labels) into human-readable
// narration for a non-developer operator. Every function here is
// DISPLAY-ONLY: it reads already-persisted, real data (the exact state/
// tool/message case_runner.py wrote) and rephrases it - it never invents
// an outcome, a confidence, or a verifier result. Technical names are
// always still available as secondary metadata, never hidden.

export const STAGE_LABELS: Record<string, string> = {
  order: "Order", payment: "Payment", refund: "Refund", settlement: "Settlement", bank: "Bank",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

const ROOT_CAUSE_TEXT: Record<string, string> = {
  duplicate_refund: "A refund appears to have been counted against the settlement twice",
  missing_refund_netting: "A refund exists but was never subtracted from the settlement",
  unreported_fee: "A fee or charge reduced the settlement without being recorded on the payment",
  partial_settlement_split: "This payment's proceeds were split across more than one settlement",
  currency_rounding: "A small rounding-sized difference",
  duplicate_bank_credit: "More than one bank transaction appears to reference this settlement",
  unmatched_external_deduction: "The bank credit is short for a reason not visible in any other record (e.g. a bank-side charge)",
  unknown: "No specific cause could be determined from the available evidence",
};

export function humanRootCause(cause: string | null | undefined): string {
  if (!cause) return "—";
  return ROOT_CAUSE_TEXT[cause] ?? cause.replace(/_/g, " ");
}

export function rootCauseLabel(cause: string | null | undefined): string {
  if (!cause) return "—";
  return cause.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export type CaseStatus = "fully_reconciled" | "divergent_explained" | "divergent_unresolved" | "in_progress";

export function caseStatus(
  traceStatus: string | null, outcome: string, chainAvailable: boolean,
): { status: CaseStatus; label: string; tone: "resolved" | "escalated" | "progress" } {
  if (outcome === "IN_PROGRESS") return { status: "in_progress", label: "In progress", tone: "progress" };
  if (!chainAvailable) {
    return outcome === "RESOLVED"
      ? { status: "fully_reconciled", label: "Fully reconciled", tone: "resolved" }
      : { status: "divergent_unresolved", label: "No settlement matched — unresolved", tone: "escalated" };
  }
  if (traceStatus === "clean") return { status: "fully_reconciled", label: "Fully reconciled", tone: "resolved" };
  if (outcome === "RESOLVED") return { status: "divergent_explained", label: "Divergent, but explained", tone: "resolved" };
  return { status: "divergent_unresolved", label: "Divergent and unresolved", tone: "escalated" };
}

export interface EventNarration {
  headline: string;
  detail: string | null;
  tone: "resolved" | "escalated" | "progress" | "neutral";
}

/** Turns one real AgentEvent (from_state, to_state, tool, message) into a
 * headline a non-developer can read at a glance, plus an optional detail
 * line — the technical from_state/to_state/tool stay visible separately
 * as secondary metadata; this never replaces them. */
export function narrateEvent(e: {
  from_state: string | null; to_state: string; tool: string | null; message: string | null; output_summary: string | null;
}): EventNarration {
  const { from_state: from, to_state: to, tool, message, output_summary } = e;
  const msg = message ?? "";

  if (from === null && to === "INGESTED") return { headline: "Case opened", detail: null, tone: "neutral" };
  if (to === "MATCH_ATTEMPT") return { headline: "Looking for a matching settlement", detail: null, tone: "progress" };
  if (to === "MATCHED") return { headline: "Settlement match found", detail: output_summary, tone: "progress" };
  if (to === "NO_MATCH") return { headline: "No confident settlement match found", detail: msg || null, tone: "escalated" };
  if (to === "VERIFY" && from === "MATCHED") return { headline: "Checking financial constraints", detail: null, tone: "progress" };
  if (to === "VERIFY" && (from === "ROOT_CAUSE_INVESTIGATE" || from === "DIVERGENCE_TRACE")) {
    return { headline: "Verifying the proposed cause", detail: msg || null, tone: "progress" };
  }
  if (to === "DIVERGENCE_TRACE") return { headline: "Financial constraints failed — tracing where it diverged", detail: msg || null, tone: "progress" };
  if (to === "ROOT_CAUSE_INVESTIGATE") return { headline: "AI investigating the root cause", detail: msg || null, tone: "progress" };
  if (to === "RESOLVED" && /reconciles exactly/.test(msg)) {
    return { headline: "Chain reconciles exactly — case closed safely", detail: null, tone: "resolved" };
  }
  if (to === "RESOLVED") return { headline: "Cause verified — case closed safely", detail: msg || null, tone: "resolved" };
  if (to === "ESCALATED") {
    if (/no_match:/.test(msg)) return { headline: "Escalated — no settlement match found", detail: null, tone: "escalated" };
    if (/unresolved -/.test(msg)) return { headline: "Escalated — missing evidence to trace the divergence", detail: null, tone: "escalated" };
    if (/no known deterministic cause/.test(msg)) return { headline: "Escalated — no known cause for the divergence", detail: null, tone: "escalated" };
    if (/failed verification/.test(msg)) return { headline: "Escalated — proposed cause failed verification", detail: null, tone: "escalated" };
    return { headline: "Escalated for human review", detail: msg || null, tone: "escalated" };
  }
  // Fallback for any transition not explicitly covered above.
  return { headline: `${from ?? "start"} → ${to}`, detail: msg || tool || null, tone: "neutral" };
}

export interface ExceptionExplanation {
  whatHappened: string;
  whatWasAttempted: string;
  whatFailed: string;
  whyStopped: string;
  whatEvidence: string;
  whatUncertainty: string;
  whatNext: string;
}

/** Answers the fixed set of questions a finance operator needs from an
 * exception, built entirely from the real, already-persisted fields the
 * API returns (stage_reached, divergence_stage, expected/actual/delta,
 * root_cause, confidence, verifier_result, reason) — never invents a
 * number or a cause that isn't already on the record. */
export function explainException(exc: {
  order_id: string; amount_paisa: number; stage_reached: string; divergence_stage: string | null;
  expected_amount_paisa: number | null; actual_amount_paisa: number | null; delta_paisa: number | null;
  root_cause: string | null; confidence: number | null;
  verifier_result: { passed: boolean; checks: { name: string; passed: boolean; detail: string }[] } | null;
}, formatRupees: (p: number | null) => string): ExceptionExplanation {
  const hasDivergence = exc.divergence_stage !== null && exc.expected_amount_paisa !== null;

  const whatHappened = hasDivergence
    ? `At the ${stageLabel(exc.divergence_stage!)} stage, ${formatRupees(exc.expected_amount_paisa)} was expected but ${formatRupees(exc.actual_amount_paisa)} was actually recorded — a difference of ${formatRupees(exc.delta_paisa)}.`
    : `No settlement could be confidently matched to this order (${formatRupees(exc.amount_paisa)}), so there is no financial chain to trace yet.`;

  const attemptedByStage: Record<string, string> = {
    no_match: "The deterministic matcher searched settlements and bank records for a confident candidate.",
    divergence_trace: "The financial chain was traced stage by stage to find where the numbers stopped adding up.",
    root_cause_investigate: "The deterministic rule table found no known cause, so the AI root-cause investigator was consulted with the available evidence.",
    verify: "A root cause was proposed and checked against the case's actual numbers and evidence by the verifier.",
    unknown: "The case was processed through the standard reconciliation pipeline.",
  };
  const whatWasAttempted = attemptedByStage[exc.stage_reached] ?? attemptedByStage.unknown;

  const failedByStage: Record<string, string> = {
    no_match: "No settlement or bank record matched this order with enough confidence to proceed.",
    divergence_trace: "No bank transaction evidence exists to compare against, so the trace could not continue past this point.",
    root_cause_investigate: exc.root_cause
      ? `The best available explanation ("${rootCauseLabel(exc.root_cause)}") was below the confidence needed to trust it automatically.`
      : "Neither a deterministic rule nor the AI investigator could confidently explain the gap.",
    verify: "The proposed cause did not pass the verifier's checks — see the verifier detail below.",
    unknown: "Automated resolution could not be completed.",
  };
  const whatFailed = failedByStage[exc.stage_reached] ?? failedByStage.unknown;

  const stoppedByStage: Record<string, string> = {
    no_match: "There is no safe fallback without a confident match — narration-based re-matching was evaluated for this system and found to increase false matches, so it is deliberately not used.",
    divergence_trace: "The system will not guess a cause without evidence to support it.",
    root_cause_investigate: "A low-confidence or unsupported guess is never trusted automatically — it always routes to a human instead.",
    verify: "The verifier is the final authority — no proposal, from a rule or from the AI, resolves a case unless it independently checks out.",
    unknown: "The system stopped rather than resolve on an unverified basis.",
  };
  const whyStopped = stoppedByStage[exc.stage_reached] ?? stoppedByStage.unknown;

  const whatEvidence = hasDivergence
    ? `${stageLabel(exc.divergence_stage!)} stage: expected ${formatRupees(exc.expected_amount_paisa)}, actual ${formatRupees(exc.actual_amount_paisa)}, delta ${formatRupees(exc.delta_paisa)}.`
    : "No settlement match exists, so no downstream evidence was ever gathered for this case.";

  const whatUncertainty = exc.root_cause
    ? `The system's best guess was "${rootCauseLabel(exc.root_cause)}"${exc.confidence !== null ? ` at ${(exc.confidence * 100).toFixed(0)}% confidence` : ""}, which did not clear the bar to resolve automatically.`
    : "No candidate cause could be identified from the evidence available.";

  const nextByStage: Record<string, string> = {
    no_match: "Check whether the settlement or bank statement for this order is missing or delayed, then re-run matching once it's available.",
    divergence_trace: "Locate the missing bank transaction record for this settlement, then re-run the case.",
    root_cause_investigate: "Review the evidence manually — the gap is real but its cause isn't confidently determined yet.",
    verify: "Review the proposed cause and the verifier's failed check below; correct the underlying record if it's wrong, or resolve manually if it's right.",
    unknown: "Review the case manually in Record Detail.",
  };
  const whatNext = nextByStage[exc.stage_reached] ?? nextByStage.unknown;

  return { whatHappened, whatWasAttempted, whatFailed, whyStopped, whatEvidence, whatUncertainty, whatNext };
}
