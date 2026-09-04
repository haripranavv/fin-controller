import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getOverview } from "../api";
import { useBatch } from "../context/BatchContext";
import { SeverityBadge } from "../components/Badges";
import { formatNumber, formatPercent, formatRupees, formatTime, formatTimestamp } from "../format";
import { narrateEvent } from "../narrate";
import type { OverviewResponse } from "../types";

const CONTROL_LOOP = [
  { label: "Import", detail: "Files land through the same pipeline as every batch" },
  { label: "Reconcile", detail: "Deterministic matcher links order → payment → settlement → bank" },
  { label: "Divergence", detail: "Constraint verifier flags where the chain stops adding up" },
  { label: "Investigate", detail: "Known-cause rule first; AI investigator if none applies" },
  { label: "Verify", detail: "Deterministic verifier checks the proposed cause against real numbers" },
  { label: "Close", detail: "Only a verified cause closes a case — otherwise a human reviews it" },
];

export default function Overview() {
  const { batches, selectedBatchId, setSelectedBatchId, runStatus } = useBatch();
  const navigate = useNavigate();
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const currentBatch = batches.find((b) => b.batch_id === selectedBatchId) ?? null;

  useEffect(() => {
    if (!selectedBatchId) return;
    let cancelled = false;
    setLoading(true);
    getOverview(selectedBatchId)
      .then((d) => { if (!cancelled) { setData(d); setError(null); } })
      .catch((e) => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // re-fetch when a run finishes so counts update without a manual refresh
  }, [selectedBatchId, runStatus?.running]);

  if (!selectedBatchId) {
    return (
      <div className="empty-state">
        No batch selected yet. <a href="#" onClick={(e) => { e.preventDefault(); navigate("/import"); }}>Import financial data</a> to create one,
        or generate a batch with scripts/run_orchestrator.py.
      </div>
    );
  }
  if (error) return <div className="error-state">{error}</div>;
  if (loading && !data) return <div className="loading-state">Loading control center…</div>;
  if (!data) return null;

  return (
    <>
      <h1 className="page-title">Control Center</h1>
      <p className="page-sub">What needs attention right now, and where this batch is in the operational loop.</p>

      <div className="panel control-loop-panel">
        <div className="control-loop">
          {CONTROL_LOOP.map((step, i) => (
            <div className="control-loop-step" key={step.label}>
              <div className="control-loop-n">{i + 1}</div>
              <div>
                <div className="control-loop-label">{step.label}</div>
                <div className="control-loop-detail">{step.detail}</div>
              </div>
              {i < CONTROL_LOOP.length - 1 && <div className="control-loop-arrow">→</div>}
            </div>
          ))}
        </div>
      </div>

      <div className="case-status-banner tone-progress" style={{ marginBottom: 10 }}>
        <div className="case-status-label">{data.batch_id}</div>
        <div className="case-status-sub">
          source: {data.dataset_version} · status: <strong>{currentBatch?.status ?? "unknown"}</strong> ·
          {" "}{formatNumber(data.total_cases)} cases ({formatNumber(data.resolved)} resolved, {formatNumber(data.escalated)} escalated, {formatNumber(data.in_progress)} in progress)
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">Batch summary</div>
        <div className="stat-row">
          <div className="stat-block">
            <div className="stat-label">Total cases</div>
            <div className="stat-value">{formatNumber(data.total_cases)}</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Resolved</div>
            <div className="stat-value resolved">{formatNumber(data.resolved)}</div>
            <div className="stat-sub">{formatPercent(data.resolution_rate)} of cases</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Escalated</div>
            <div className="stat-value escalated">{formatNumber(data.escalated)}</div>
            <div className="stat-sub">{formatPercent(data.exception_count / (data.total_cases || 1))} of cases</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Match rate (Rs value)</div>
            <div className="stat-value">{formatPercent(data.monetary_resolution_rate)}</div>
            <div className="stat-sub">{formatRupees(data.resolved_value_paisa)} / {formatRupees(data.total_value_paisa)}</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Rs affected (open exceptions)</div>
            <div className="stat-value escalated">{formatRupees(data.exception_value_paisa)}</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Throughput</div>
            <div className="stat-value">{data.throughput_cases_per_sec ? data.throughput_cases_per_sec.toFixed(1) : "—"}</div>
            <div className="stat-sub">cases / sec (this batch's run)</div>
          </div>
        </div>
      </div>

      <div className="two-col">
        <div className="panel" style={{ padding: 0 }}>
          <div className="panel-title" style={{ padding: "11px 14px 0" }}>
            Attention queue ({data.attention_cases.length})
            {data.highlighted_case_id && (
              <button
                className="primary" style={{ float: "right", padding: "3px 10px", fontSize: 11.5 }}
                onClick={() => navigate(`/investigation/${data.highlighted_case_id}`)}
              >
                Open top case →
              </button>
            )}
          </div>
          <table>
            <thead><tr><th>Order</th><th>Severity</th><th className="num">Amount</th><th>Reason</th></tr></thead>
            <tbody>
              {data.attention_cases.map((c) => (
                <tr key={c.case_id} className="clickable" onClick={() => navigate(`/investigation/${c.case_id}`)}
                    style={c.case_id === data.highlighted_case_id ? { background: "var(--accent-dim)" } : undefined}>
                  <td className="mono">{c.order_id}</td>
                  <td><SeverityBadge severity={c.severity} /></td>
                  <td className="num">{formatRupees(c.amount_paisa)}</td>
                  <td>{c.reason_category}</td>
                </tr>
              ))}
              {data.attention_cases.length === 0 && <tr><td colSpan={4} className="empty-state">Nothing open — every case in this batch is resolved.</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="panel" style={{ padding: 0 }}>
          <div className="panel-title" style={{ padding: "11px 14px 0" }}>Recent activity</div>
          <div className="event-log" style={{ maxHeight: 320, overflowY: "auto" }}>
            {data.recent_events.map((e) => {
              const n = narrateEvent(e);
              return (
                <div key={e.id} className={`activity-row tone-${n.tone}`} style={{ cursor: "pointer" }} onClick={() => navigate(`/cases/${e.case_id}`)}>
                  <div className="activity-time mono">{formatTime(e.created_at)}</div>
                  <div>
                    <div className="activity-headline">{n.headline}</div>
                    <div className="activity-meta"><span className="mono">{e.case_id}</span></div>
                  </div>
                </div>
              );
            })}
            {data.recent_events.length === 0 && <div className="empty-state">No activity recorded yet.</div>}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">What did the controller do on this run?</div>
        <div className="stat-row">
          <div className="stat-block">
            <div className="stat-label">Chain reconciled exactly</div>
            <div className="stat-value resolved">{formatNumber(data.clean_resolved)}</div>
            <div className="stat-sub">no divergence, no investigation needed</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Deterministic resolutions</div>
            <div className="stat-value resolved">{formatNumber(data.deterministic_resolved)}</div>
            <div className="stat-sub">divergence explained by a known-cause rule</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">AI-assisted investigations</div>
            <div className="stat-value">
              {formatNumber(data.ai_resolved)} <span className="pill ai" style={{ verticalAlign: "middle" }}>AI</span>
            </div>
            <div className="stat-sub">resolved after the AI investigator proposed a verified cause</div>
          </div>
          <div className="stat-block">
            <div className="stat-label">Escalations</div>
            <div className="stat-value escalated">{formatNumber(data.escalated)}</div>
            <div className="stat-sub">{formatRupees(data.exception_value_paisa)} open exception value</div>
          </div>
        </div>

        {data.top_escalation_reasons.length > 0 && (
          <>
            <div className="panel-title" style={{ marginTop: 14 }}>Top exception categories</div>
            <table>
              <thead><tr><th>Reason</th><th className="num">Cases</th><th className="num">Value</th></tr></thead>
              <tbody>
                {data.top_escalation_reasons.map((r) => (
                  <tr key={r.reason_category}>
                    <td>{r.reason_category}</td>
                    <td className="num">{formatNumber(r.count)}</td>
                    <td className="num">{formatRupees(r.value_paisa)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      {data.last_evaluation.length > 0 && (
        <div className="panel">
          <div className="panel-title">Last held-out evaluation (ground-truth scored, offline — scripts/run_evaluation.py)</div>
          <table>
            <thead>
              <tr>
                <th>Mode</th><th className="num">Records</th><th className="num">Match rate</th>
                <th className="num">Match rate (Rs)</th><th className="num">Precision</th><th className="num">Recall</th>
                <th className="num">False-match rate</th><th className="num">AI-assisted</th><th className="num">Exceptions</th>
                <th>Computed</th>
              </tr>
            </thead>
            <tbody>
              {data.last_evaluation.map((ev) => (
                <tr key={ev.mode}>
                  <td>{ev.mode === "ai_enhanced" ? <span className="pill ai">AI-enhanced</span> : <span className="pill">baseline</span>}</td>
                  <td className="num">{formatNumber(ev.records_processed)}</td>
                  <td className="num">{formatPercent(ev.match_rate)}</td>
                  <td className="num">{formatPercent(ev.match_rate_by_value)}</td>
                  <td className="num">{formatPercent(ev.precision)}</td>
                  <td className="num">{formatPercent(ev.recall)}</td>
                  <td className="num">{formatPercent(ev.false_match_rate)}</td>
                  <td className="num">{formatPercent(ev.ai_assisted_resolution_rate)}</td>
                  <td className="num">{formatNumber(ev.exception_count)} ({formatRupees(ev.exception_value_paisa)})</td>
                  <td className="mono" style={{ fontSize: 11 }}>{formatTimestamp(ev.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="page-sub" style={{ marginTop: 10, marginBottom: 0 }}>
            This is a scored comparison against isolated ground truth, computed offline — never part of live case
            processing above. See docs/ARCHITECTURE_NOTES.md's Milestone 10 section for the full analysis.
          </p>
        </div>
      )}

      <div className="panel">
        <div className="panel-title">Recent batches</div>
        <table>
          <thead><tr><th>Batch</th><th>Source</th><th className="num">Cases</th><th>Status</th></tr></thead>
          <tbody>
            {batches.slice(0, 5).map((b) => (
              <tr key={b.batch_id} className="clickable" style={b.batch_id === selectedBatchId ? { background: "var(--accent-dim)" } : undefined}
                  onClick={() => setSelectedBatchId(b.batch_id)}>
                <td className="mono">{b.batch_id}</td>
                <td className="mono">{b.dataset_version}</td>
                <td className="num">{formatNumber(b.total_cases)}</td>
                <td>{b.status}</td>
              </tr>
            ))}
            {batches.length === 0 && <tr><td colSpan={4} className="empty-state">No batches yet.</td></tr>}
          </tbody>
        </table>
        {batches.length > 5 && (
          <button className="link-btn" style={{ marginTop: 8 }} onClick={() => navigate("/batches")}>View all {batches.length} batches →</button>
        )}
      </div>
    </>
  );
}
