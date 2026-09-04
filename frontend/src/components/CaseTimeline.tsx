// Shared financial-chain timeline + investigation summary, used by both
// the Investigation screen and Record Detail (Record Detail is "the
// complete case file" and needs the same chain/first-divergence view,
// not a separate reimplementation of it).
import { formatRupees, formatTimestamp } from "../format";
import { caseStatus, humanRootCause, rootCauseLabel, stageLabel } from "../narrate";
import type { InvestigationDetail, PrivacyBoundary, StageDTO } from "../types";

export function StatusBanner({ detail }: { detail: InvestigationDetail }) {
  const s = caseStatus(detail.trace_status, detail.outcome, detail.chain_available);
  return (
    <div className={`case-status-banner tone-${s.tone}`}>
      <div className="case-status-label">{s.label}</div>
      <div className="case-status-sub">
        {detail.order_id} · {formatRupees(detail.amount_paisa)}
        {detail.first_divergence_stage && <> · first divergence at <strong>{stageLabel(detail.first_divergence_stage)}</strong></>}
      </div>
    </div>
  );
}

export function ChainTimeline({ detail }: { detail: InvestigationDetail }) {
  if (!detail.chain_available) {
    return (
      <div className="empty-state">
        No settlement was matched for this case, so there is no chain to trace — see the event trace for why.
      </div>
    );
  }
  return (
    <div className="chain-timeline">
      {detail.chain.map((stage, i) => (
        <TimelineHop key={stage.stage} stage={stage} isLast={i === detail.chain.length - 1} />
      ))}
    </div>
  );
}

export function InvestigationSummary({ detail }: { detail: InvestigationDetail }) {
  const inv = detail.investigation;
  const firstStage = detail.chain.find((s) => s.is_first_divergence);
  const failedCheck = detail.events.flatMap((e) => e.verifier_result?.checks ?? []).find((c) => !c.passed);
  const passedVerifier = detail.outcome === "RESOLVED";
  const stageName = firstStage ? stageLabel(firstStage.stage) : detail.first_divergence_stage ? stageLabel(detail.first_divergence_stage) : "—";
  const expected = inv ? formatRupees(inv.expected_amount_paisa) : (firstStage ? formatRupees(firstStage.expected_paisa) : "—");
  const actual = inv ? formatRupees(inv.actual_amount_paisa) : (firstStage ? formatRupees(firstStage.actual_paisa) : "—");
  const delta = inv ? formatRupees(inv.delta_paisa) : (firstStage ? formatRupees(firstStage.delta_paisa) : "—");

  return (
    <>
      {/* The single most important fact on this screen: where the money
          stopped adding up, and by how much. Deliberately the largest,
          highest-contrast block here - everything else is detail. */}
      <div className="divergence-hero">
        <div className="divergence-hero-label">First divergence</div>
        <div className="divergence-hero-stage">{stageName}</div>
        <div className="divergence-hero-figures">
          <div className="divergence-hero-figure">
            <div className="divergence-hero-figure-label">Expected</div>
            <div className="divergence-hero-figure-value mono">{expected}</div>
          </div>
          <div className="divergence-hero-figure">
            <div className="divergence-hero-figure-label">Actual</div>
            <div className="divergence-hero-figure-value mono">{actual}</div>
          </div>
          <div className="divergence-hero-figure">
            <div className="divergence-hero-figure-label">Delta</div>
            <div className="divergence-hero-figure-value mono divergence-hero-delta">{delta}</div>
          </div>
        </div>
      </div>

      <div className="summary-grid">
        <SummaryRow label="Root cause">
          {inv?.root_cause ? (
            <>
              <strong>{rootCauseLabel(inv.root_cause)}</strong>
              <div className="summary-explain">{humanRootCause(inv.root_cause)}</div>
            </>
          ) : "Not determined"}
        </SummaryRow>
        <SummaryRow label={detail.resolved_via === "ai" ? "AI confidence" : "Confidence"}>
          {inv?.confidence !== null && inv?.confidence !== undefined ? `${(inv.confidence * 100).toFixed(0)}%` : "—"}
          {detail.resolved_via && <span className="pill" style={{ marginLeft: 6 }}>{detail.resolved_via === "ai" ? "AI proposal" : detail.resolved_via === "deterministic" ? "rule" : detail.resolved_via}</span>}
          {detail.resolved_via === "ai" && (
            <div className="summary-explain">Confidence is not authorization — only the deterministic verifier below can close a case.</div>
          )}
        </SummaryRow>
        <SummaryRow label="Verifier">
          <span className={passedVerifier ? "badge resolved" : "badge escalated"}>{passedVerifier ? "PASSED" : "FAILED"}</span>
          {failedCheck && <div className="summary-explain">{failedCheck.name}: {failedCheck.detail}</div>}
        </SummaryRow>
        <SummaryRow label="Final decision">
          <span className={`badge ${passedVerifier ? "resolved" : "escalated"}`}>{passedVerifier ? "RESOLVED" : "ESCALATED"}</span>
        </SummaryRow>
        <SummaryRow label="Evidence">
          {firstStage && firstStage.evidence.length > 0 ? (
            <span className="mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{firstStage.evidence.join(", ")}</span>
          ) : "No supporting records cited"}
        </SummaryRow>
        <SummaryRow label="Downstream impact">
          {detail.downstream_impact.length === 0
            ? "None — divergence did not propagate further down the chain"
            : detail.downstream_impact.map((s) => `${stageLabel(s.stage)} (Δ ${formatRupees(s.delta_paisa)})`).join(", ")}
        </SummaryRow>
      </div>
    </>
  );
}

/** AI INVESTIGATION -> ROOT-CAUSE PROPOSAL -> DETERMINISTIC VERIFIER ->
 * RESOLVED/ESCALATED, for any case where the AI investigator was actually
 * invoked (detail.ai_was_invoked) - never shown for a clean or
 * deterministically-resolved case, since the AI never touched those.
 * Gemini only ever proposes; this makes the "Gemini never directly
 * resolves a case" rule visible, not just true in the backend. */
export function AIDecisionFlow({ detail }: { detail: InvestigationDetail }) {
  const inv = detail.investigation;
  const resolved = detail.outcome === "RESOLVED";
  const steps = [
    { label: "AI investigation", sub: "Gemini root-cause investigator", tech: "ROOT_CAUSE_INVESTIGATE", tone: "progress" as const },
    {
      label: "Root-cause proposal",
      sub: inv?.root_cause ? `${rootCauseLabel(inv.root_cause)}${inv.confidence !== null ? ` · ${(inv.confidence * 100).toFixed(0)}% confidence` : ""}` : "no proposal recorded",
      tech: "proposal (not yet verified)", tone: "progress" as const,
    },
    {
      label: "Deterministic verifier",
      sub: resolved ? "Independently checked — passed" : "Independently checked — did not pass",
      tech: "constraint_verifier", tone: resolved ? "resolved" as const : "escalated" as const,
    },
    {
      label: resolved ? "Resolved" : "Escalated for human review",
      sub: resolved ? "Case closed — verified explanation on record" : "A human reviews before this case closes",
      tech: detail.state, tone: resolved ? "resolved" as const : "escalated" as const,
    },
  ];
  return (
    <div className="decision-flow">
      {steps.map((s, i) => (
        <div className="decision-flow-step" key={s.label}>
          <div className={`decision-flow-marker tone-${s.tone}`}>{i + 1}</div>
          <div>
            <div className="decision-flow-label">{s.label}</div>
            <div className="decision-flow-sub">{s.sub}</div>
            <div className="decision-flow-tech mono">{s.tech}</div>
          </div>
          {i < steps.length - 1 && <div className="decision-flow-arrow">↓</div>}
        </div>
      ))}
    </div>
  );
}

/** What was actually sent to Gemini for this case - detail.privacy.
 * evidence_sent is the literal payload app.rootcause.evidence.build_evidence
 * produced (recomputed for display from the API, not a description of
 * it) - see PrivacyBoundary in types.ts / schemas.py. Never claims more
 * than the booleans below actually mean: each one reflects what the real
 * evidence-builder does and does not include, not an aspirational policy. */
export function PrivacyBoundaryPanel({ privacy }: { privacy: PrivacyBoundary }) {
  const rows: { label: string; sent: boolean; note: string }[] = [
    { label: "Raw uploaded files", sent: privacy.raw_files_sent, note: "the original CSV/database rows are never forwarded" },
    { label: "Ground truth", sent: privacy.ground_truth_sent, note: "evaluation labels live in an isolated schema no API path can reach" },
    { label: "Unnecessary PII", sent: privacy.unnecessary_pii_sent, note: "only ids, amounts, stages and narration text needed for this case" },
    { label: "Structured evidence", sent: privacy.structured_evidence_sent, note: "the minimum bounded evidence set for this investigation" },
  ];
  return (
    <div className="privacy-panel">
      <div className="privacy-rows">
        {rows.map((r) => (
          <div className="privacy-row" key={r.label}>
            <span className={`privacy-dot ${r.sent ? "sent" : "not-sent"}`} />
            <span className="privacy-row-label">{r.label}</span>
            <span className={`privacy-row-state ${r.sent ? "sent" : "not-sent"}`}>{r.sent ? "SENT" : "NOT SENT"}</span>
            <span className="privacy-row-note">{r.note}</span>
          </div>
        ))}
      </div>
      <details className="privacy-evidence">
        <summary>View the exact evidence payload sent ({privacy.evidence_sent.length} item{privacy.evidence_sent.length === 1 ? "" : "s"})</summary>
        <pre className="mono">{JSON.stringify(privacy.evidence_sent, null, 2)}</pre>
      </details>
    </div>
  );
}

function SummaryRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="summary-row">
      <div className="summary-label">{label}</div>
      <div className="summary-value">{children}</div>
    </div>
  );
}

function TimelineHop({ stage, isLast }: { stage: StageDTO; isLast: boolean }) {
  const statusTag = stage.is_first_divergence ? "first divergence" : stage.consistent ? "pass" : "fail";
  return (
    <div className="timeline-hop">
      <div className="timeline-marker-col">
        <div className={`timeline-marker ${stage.is_first_divergence ? "first-divergence" : stage.consistent ? "pass" : "fail"}`} />
        {!isLast && <div className="timeline-line" />}
      </div>
      <div className={`timeline-content ${stage.is_first_divergence ? "first-divergence" : ""}`}>
        <div className="timeline-hop-header">
          <span className="timeline-hop-name">{stageLabel(stage.stage)}</span>
          <span className={`badge ${stage.is_first_divergence ? "escalated" : stage.consistent ? "resolved" : "escalated"}`}>{statusTag}</span>
          {stage.timestamp && <span className="timeline-hop-time mono">{formatTimestamp(stage.timestamp)}</span>}
        </div>
        <div className="timeline-hop-note">{stage.note}</div>
        <div className="timeline-hop-figures">
          <span><span className="figure-label">Expected</span> {stage.expected_paisa !== null ? formatRupees(stage.expected_paisa) : "—"}</span>
          <span><span className="figure-label">Actual</span> {stage.actual_paisa !== null ? formatRupees(stage.actual_paisa) : "—"}</span>
          <span className={stage.delta_paisa ? "figure-delta-nonzero" : undefined}>
            <span className="figure-label">Delta</span> {stage.delta_paisa !== null ? formatRupees(stage.delta_paisa) : "—"}
          </span>
        </div>
      </div>
    </div>
  );
}
