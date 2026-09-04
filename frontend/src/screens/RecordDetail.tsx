import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getCaseDetail, getCaseInvestigation } from "../api";
import { AIDecisionFlow, ChainTimeline, InvestigationSummary, PrivacyBoundaryPanel, StatusBanner } from "../components/CaseTimeline";
import { formatRupees, formatTimestamp } from "../format";

// record.detail is a raw {field_name: value} dict from the backend - it
// can carry other *_paisa integer fields (fee_paisa, tax_on_fee_paisa,
// fee_deducted_paisa) alongside plain strings/dates. This is the one
// place those get relabeled (never "fee paisa") and reformatted as
// rupees (never a raw integer) before display - everything else falls
// through to a plain, title-cased label.
const DATE_DETAIL_KEYS = new Set(["created_at", "period_start", "period_end", "value_date"]);

function detailLabel(key: string): string {
  const base = key.endsWith("_paisa") ? key.slice(0, -"_paisa".length) : key;
  return base.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function detailValue(key: string, v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (key.endsWith("_paisa")) return formatRupees(Number(v));
  if (DATE_DETAIL_KEYS.has(key)) return formatTimestamp(String(v));
  return String(v);
}
import { narrateEvent } from "../narrate";
import type { CaseDetail, FinancialRecord, InvestigationDetail } from "../types";

export default function RecordDetail() {
  const { caseId } = useParams();
  const [data, setData] = useState<CaseDetail | null>(null);
  const [inv, setInv] = useState<InvestigationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;
    Promise.all([getCaseDetail(caseId), getCaseInvestigation(caseId)])
      .then(([d, i]) => { if (!cancelled) { setData(d); setInv(i); setError(null); } })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [caseId]);

  if (error) return <div className="error-state">{error}</div>;
  if (!data || !inv) return <div className="loading-state">Loading…</div>;

  return (
    <>
      <div className="breadcrumb">
        <Link to="/reconciliation">Cases</Link> / <Link to={`/investigation/${data.case_id}`}>Investigation</Link> / Record Detail
      </div>
      <h1 className="page-title">{data.order_id}</h1>
      <p className="page-sub">
        {data.case_id} — batch {data.batch_id} · created {formatTimestamp(data.created_at)}, updated {formatTimestamp(data.updated_at)}
      </p>

      <StatusBanner detail={inv} />

      <div className="two-col">
        <div>
          {inv.trace_status === "diverged" && (
            <div className="panel">
              <div className="panel-title">Investigation</div>
              <InvestigationSummary detail={inv} />
            </div>
          )}

          {inv.ai_was_invoked && (
            <div className="panel">
              <div className="panel-title">AI investigation → verifier → decision</div>
              <AIDecisionFlow detail={inv} />
            </div>
          )}

          {inv.privacy && (
            <div className="panel">
              <div className="panel-title">AI privacy boundary — what was sent to Gemini</div>
              <PrivacyBoundaryPanel privacy={inv.privacy} />
            </div>
          )}

          <div className="panel">
            <div className="panel-title">Financial chain</div>
            <ChainTimeline detail={inv} />
          </div>

          <div className="panel">
            <div className="panel-title">Source records</div>
            {data.order && <RecordCard record={data.order} />}
            {data.payments.map((p) => <RecordCard key={p.record_id} record={p} />)}
            {data.refunds.map((r) => <RecordCard key={r.record_id} record={r} />)}
            {data.settlement && <RecordCard record={data.settlement} />}
            {data.bank_txns.map((b) => <RecordCard key={b.record_id} record={b} />)}
            {!data.order && <div className="empty-state">No order record found (unexpected).</div>}
          </div>

          <div className="panel">
            <div className="panel-title">Matches ({data.matches.length})</div>
            {data.matches.length === 0 && <div className="empty-state">No accepted matches — case never matched a settlement.</div>}
            {data.matches.length > 0 && (
              <table>
                <thead><tr><th>Source</th><th>Target</th><th>Method</th><th className="num">Score</th></tr></thead>
                <tbody>
                  {data.matches.map((m, i) => (
                    <tr key={i}>
                      <td className="mono">{m.source_type}:{m.source_id}</td>
                      <td className="mono">{m.target_type}:{m.target_id}</td>
                      <td>{m.method}</td>
                      <td className="num">{m.score.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {data.exception && (
            <div className="panel">
              <div className="panel-title">Exception</div>
              <div className="kv-grid">
                <div className="kv-item"><div className="kv-label">Severity</div><div className="kv-value">{data.exception.severity}</div></div>
                <div className="kv-item"><div className="kv-label">Status</div><div className="kv-value">{data.exception.status}</div></div>
                <div className="kv-item"><div className="kv-label">Amount</div><div className="kv-value">{formatRupees(data.exception.amount_paisa)}</div></div>
              </div>
              <p style={{ fontSize: 12.5, marginTop: 8 }}>{data.exception.reason}</p>
            </div>
          )}
        </div>

        <div>
          <div className="panel">
            <div className="panel-title">Execution trace ({data.events.length} steps)</div>
            <div className="narrative-log">
              {data.events.map((e) => {
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
        </div>
      </div>
    </>
  );
}

function RecordCard({ record }: { record: FinancialRecord }) {
  return (
    <div className="record-card">
      <div className="record-card-title">{record.record_type.replace(/_/g, " ")} — {record.record_id}</div>
      <div className="kv-grid">
        <div className="kv-item"><div className="kv-label">Amount</div><div className="kv-value">{formatRupees(record.amount_paisa)}</div></div>
        {Object.entries(record.detail).map(([k, v]) => (
          <div className="kv-item" key={k}>
            <div className="kv-label">{detailLabel(k)}</div>
            <div className="kv-value" style={{ fontSize: 11.5 }}>{detailValue(k, v)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
