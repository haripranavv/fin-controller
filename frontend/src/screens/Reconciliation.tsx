import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listCases } from "../api";
import { useBatch } from "../context/BatchContext";
import { StateBadge, ResolvedViaPill, SeverityBadge } from "../components/Badges";
import { formatRupees, formatTimestamp } from "../format";
import type { CaseListItem } from "../types";

const STATES = ["", "INGESTED", "MATCH_ATTEMPT", "MATCHED", "NO_MATCH", "VERIFY", "DIVERGENCE_TRACE",
  "ROOT_CAUSE_INVESTIGATE", "RESOLVED", "ESCALATED"];

export default function Reconciliation() {
  const { selectedBatchId } = useBatch();
  const navigate = useNavigate();
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<CaseListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedBatchId) return;
    let cancelled = false;
    setLoading(true);
    listCases({ batchId: selectedBatchId, state: state || undefined, q: q || undefined, limit: 300 })
      .then((r) => { if (!cancelled) { setData(r.cases); setTotal(r.total); setError(null); } })
      .catch((e) => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [selectedBatchId, state, q]);

  if (!selectedBatchId) return <div className="empty-state">No batch selected.</div>;

  return (
    <>
      <h1 className="page-title">Cases</h1>
      <p className="page-sub">Dense, filterable case table — order, outcome, finding, and resolution path for every case.</p>

      <div className="filters">
        <select value={state} onChange={(e) => setState(e.target.value)}>
          {STATES.map((s) => <option key={s} value={s}>{s || "All states"}</option>)}
        </select>
        <input type="text" placeholder="search order_id…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="spacer" />
        <span className="count-tag">{loading ? "loading…" : `${total} case${total === 1 ? "" : "s"}`}</span>
      </div>

      {error && <div className="error-state">{error}</div>}

      {!error && (
        <div className="panel" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>Order</th><th>Outcome</th><th>Finding</th><th>Resolution path</th>
                <th className="num">Amount</th><th>Severity</th><th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.map((c) => (
                <tr key={c.case_id} className="clickable" onClick={() => navigate(`/cases/${c.case_id}`)}>
                  <td>
                    <div className="mono">{c.order_id}</div>
                    <div className="recon-secondary-id mono">{c.case_id} · {c.state}</div>
                  </td>
                  <td><StateBadge state={c.outcome} /></td>
                  <td className="recon-finding">{c.finding}</td>
                  <td><ResolvedViaPill via={c.resolved_via} /></td>
                  <td className="num">{formatRupees(c.amount_paisa)}</td>
                  <td><SeverityBadge severity={c.severity} /></td>
                  <td className="mono" style={{ fontSize: 11 }}>{formatTimestamp(c.updated_at)}</td>
                </tr>
              ))}
              {data.length === 0 && !loading && (
                <tr><td colSpan={7} className="empty-state">No cases match this filter.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
