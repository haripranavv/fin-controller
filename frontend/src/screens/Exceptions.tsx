import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listExceptions } from "../api";
import { useBatch } from "../context/BatchContext";
import { SeverityBadge } from "../components/Badges";
import { formatRupees, formatTimestamp } from "../format";
import { explainException } from "../narrate";
import type { ExceptionListItem, ExceptionListResponse } from "../types";

const SEVERITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

/** Composite priority, lowest first: severity is the primary axis, then
 * a case whose proposed cause already failed verification (nothing left
 * to wait on), then a low-confidence proposal (< 50%), then value at
 * stake. Purely a display-order decision over data already returned by
 * the API - no new fields, no backend change. */
function priorityScore(exc: ExceptionListItem): number {
  const severity = SEVERITY_RANK[exc.severity] ?? 4;
  const verifierFailed = exc.verifier_result?.passed === false ? 0 : 1;
  const lowConfidence = exc.confidence !== null && exc.confidence < 0.5 ? 0 : 1;
  return severity * 1000 + verifierFailed * 100 + lowConfidence * 10;
}

export default function Exceptions() {
  const { selectedBatchId } = useBatch();
  const navigate = useNavigate();
  const [data, setData] = useState<ExceptionListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedBatchId) return;
    let cancelled = false;
    listExceptions(selectedBatchId)
      .then((r) => { if (!cancelled) { setData(r); setError(null); } })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [selectedBatchId]);

  if (!selectedBatchId) return <div className="empty-state">No batch selected.</div>;
  if (error) return <div className="error-state">{error}</div>;
  if (!data) return <div className="loading-state">Loading…</div>;

  return (
    <>
      <h1 className="page-title">Exceptions</h1>
      <p className="page-sub">
        {data.total} open exception{data.total === 1 ? "" : "s"} — {formatRupees(data.total_value_paisa)} total value.
        Sorted by priority — severity, then a proposal that already failed verification, then low confidence, then value at stake.
        Every exception answers what happened, what was attempted, why automation stopped, and what a human should do next.
      </p>

      <div className="panel" style={{ padding: 0 }}>
        {data.exceptions.length === 0 && <div className="empty-state">No open exceptions for this batch.</div>}
        {[...data.exceptions].sort((a, b) => priorityScore(a) - priorityScore(b) || b.amount_paisa - a.amount_paisa).map((exc) => (
          <ExceptionCard
            key={exc.case_id}
            exc={exc}
            open={expanded === exc.case_id}
            onToggle={() => setExpanded(expanded === exc.case_id ? null : exc.case_id)}
            onOpenCase={() => navigate(`/cases/${exc.case_id}`)}
          />
        ))}
      </div>
    </>
  );
}

function ExceptionCard({ exc, open, onToggle, onOpenCase }: {
  exc: ExceptionListItem; open: boolean; onToggle: () => void; onOpenCase: () => void;
}) {
  const explain = explainException(exc, (p) => formatRupees(p));
  return (
    <div className="exception-card">
      <div className="clickable exception-summary-row" onClick={onToggle}>
        <span className="mono">{exc.order_id}</span>
        <SeverityBadge severity={exc.severity} />
        <span className="num mono">{formatRupees(exc.amount_paisa)}</span>
        <span className="exception-what-happened">{explain.whatHappened}</span>
        <span className="mono" style={{ fontSize: 11, textAlign: "right" }}>{formatTimestamp(exc.created_at)}</span>
      </div>
      {open && (
        <div className="exception-detail">
          <div className="qa-grid">
            <QA question="What happened?">{explain.whatHappened}</QA>
            <QA question="What was attempted?">{explain.whatWasAttempted}</QA>
            <QA question="What failed?">{explain.whatFailed}</QA>
            <QA question="Why did automation stop?">{explain.whyStopped}</QA>
            <QA question="What evidence exists?">{explain.whatEvidence}</QA>
            <QA question="What uncertainty remains?">{explain.whatUncertainty}</QA>
            <QA question="What should a human do next?">{explain.whatNext}</QA>
          </div>

          <div className="kv-grid" style={{ margin: "10px 0" }}>
            <div className="kv-item"><div className="kv-label">AI proposal</div><div className="kv-value">{exc.root_cause ?? "none / not reached"}</div></div>
            <div className="kv-item"><div className="kv-label">Confidence</div><div className="kv-value">{exc.confidence !== null ? `${(exc.confidence * 100).toFixed(0)}%` : "—"}</div></div>
            <div className="kv-item"><div className="kv-label">Status</div><div className="kv-value">{exc.status}</div></div>
          </div>

          <div className="exception-raw-reason"><strong>System log: </strong>{exc.reason}</div>

          {exc.verifier_result ? (
            <div className="verifier-checks" style={{ marginTop: 8 }}>
              <div className="kv-label" style={{ marginBottom: 4 }}>Verifier detail</div>
              {exc.verifier_result.checks.map((c) => (
                <div key={c.name} className={`verifier-check ${c.passed ? "pass" : "fail"}`}>
                  <span className="check-icon">{c.passed ? "✓" : "✗"}</span>
                  <span>{c.name}</span>
                  <span className="check-detail">— {c.detail}</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 8 }}>No verifier result recorded — case never reached a proposal to verify.</div>
          )}
          <button style={{ marginTop: 10 }} onClick={onOpenCase}>Open full record →</button>
        </div>
      )}
    </div>
  );
}

function QA({ question, children }: { question: string; children: React.ReactNode }) {
  return (
    <div className="qa-item">
      <div className="qa-question">{question}</div>
      <div className="qa-answer">{children}</div>
    </div>
  );
}
