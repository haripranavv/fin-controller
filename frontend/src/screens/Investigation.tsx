import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { getCaseInvestigation, listCases } from "../api";
import { useBatch } from "../context/BatchContext";
import { StateBadge } from "../components/Badges";
import { AIDecisionFlow, ChainTimeline, InvestigationSummary, PrivacyBoundaryPanel, StatusBanner } from "../components/CaseTimeline";
import { formatTimestamp } from "../format";
import { narrateEvent } from "../narrate";
import type { CaseListItem, InvestigationDetail } from "../types";

export default function Investigation() {
  const { selectedBatchId } = useBatch();
  const { caseId } = useParams();
  const navigate = useNavigate();
  const [cases, setCases] = useState<CaseListItem[]>([]);
  const [q, setQ] = useState("");
  const [detail, setDetail] = useState<InvestigationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedBatchId) return;
    listCases({ batchId: selectedBatchId, q: q || undefined, limit: 300 }).then((r) => setCases(r.cases)).catch(() => {});
  }, [selectedBatchId, q]);

  useEffect(() => {
    if (!caseId) { setDetail(null); return; }
    let cancelled = false;
    getCaseInvestigation(caseId)
      .then((d) => { if (!cancelled) { setDetail(d); setError(null); } })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [caseId]);

  if (!selectedBatchId) return <div className="empty-state">No batch selected.</div>;

  return (
    <>
      <h1 className="page-title">Investigation</h1>
      <p className="page-sub">Pick a case to see its financial chain as a timeline, the first point of divergence, and how it was resolved.</p>

      <div className="two-col" style={{ gridTemplateColumns: "260px 1fr" }}>
        <div className="panel" style={{ padding: 0, maxHeight: 640, overflowY: "auto" }}>
          <div style={{ padding: 10, borderBottom: "1px solid var(--border)" }}>
            <input type="text" placeholder="search order_id…" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%" }} />
          </div>
          <table>
            <thead><tr><th>Order</th><th>Outcome</th></tr></thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.case_id} className="clickable" onClick={() => navigate(`/investigation/${c.case_id}`)}
                    style={c.case_id === caseId ? { background: "var(--accent-dim)" } : undefined}>
                  <td className="mono">{c.order_id}</td>
                  <td><StateBadge state={c.outcome} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div>
          {!caseId && <div className="panel"><div className="empty-state">Pick a case on the left to open its investigation timeline.</div></div>}
          {error && <div className="error-state">{error}</div>}
          {detail && <InvestigationBody detail={detail} />}
        </div>
      </div>
    </>
  );
}

function InvestigationBody({ detail }: { detail: InvestigationDetail }) {
  const isDivergent = detail.trace_status === "diverged";
  return (
    <>
      <StatusBanner detail={detail} />

      {/* The first divergence, root cause, confidence, verifier result and
          downstream impact are the single most important thing on this
          screen for a divergent case - shown immediately, above the
          stage-by-stage chain, not after it. */}
      {isDivergent && (
        <div className="panel investigation-summary">
          <div className="panel-title">First divergence</div>
          <InvestigationSummary detail={detail} />
        </div>
      )}

      {detail.ai_was_invoked && (
        <div className="panel">
          <div className="panel-title">AI investigation → verifier → decision</div>
          <AIDecisionFlow detail={detail} />
        </div>
      )}

      {detail.privacy && (
        <div className="panel">
          <div className="panel-title">AI privacy boundary — what was sent to Gemini</div>
          <PrivacyBoundaryPanel privacy={detail.privacy} />
        </div>
      )}

      <div className="panel">
        <div className="panel-title">Financial chain: order → payment → refund → settlement → bank</div>
        <ChainTimeline detail={detail} />
      </div>

      <div className="panel">
        <div className="panel-title">Case narrative ({detail.events.length} steps)</div>
        <div className="narrative-log">
          {detail.events.map((e) => {
            const n = narrateEvent(e);
            return (
              <div key={e.id} className={`narrative-step tone-${n.tone}`}>
                <div className="narrative-headline">{n.headline}</div>
                {n.detail && <div className="narrative-detail">{n.detail}</div>}
                <div className="narrative-meta">
                  <span className="mono">{e.from_state ?? "start"} → {e.to_state}</span>
                  {e.tool && <span className="pill">{e.tool}</span>}
                  <span className="mono">{formatTimestamp(e.created_at)}</span>
                </div>
                {e.verifier_result && (
                  <div className="verifier-checks">
                    {e.verifier_result.checks.map((c) => (
                      <div key={c.name} className={`verifier-check ${c.passed ? "pass" : "fail"}`}>
                        <span className="check-icon">{c.passed ? "✓" : "✗"}</span>
                        <span>{c.name}</span>
                        <span className="check-detail">— {c.detail}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
