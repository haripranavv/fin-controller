import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listImportJobs } from "../api";
import { useBatch } from "../context/BatchContext";
import { formatNumber, formatTimestamp } from "../format";
import type { ImportJobResponse } from "../types";

const JOB_STATUS_TONE: Record<string, string> = {
  QUEUED: "progress", VALIDATING: "progress", IMPORTING: "progress", READY: "resolved", FAILED: "escalated",
};

export default function Batches() {
  const { batches, selectedBatchId, setSelectedBatchId } = useBatch();
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<ImportJobResponse[]>([]);

  useEffect(() => {
    listImportJobs().then(setJobs).catch(() => {});
  }, []);

  return (
    <>
      <h1 className="page-title">Batches</h1>
      <p className="page-sub">Every persisted batch — generated or imported — and the import jobs that created them.</p>

      <div className="panel" style={{ padding: 0 }}>
        <div className="panel-title" style={{ padding: "11px 14px 0" }}>Batches ({batches.length})</div>
        <table>
          <thead><tr><th>Batch</th><th>Source</th><th className="num">Cases</th><th className="num">Resolved</th><th className="num">Escalated</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.batch_id} className="clickable" style={b.batch_id === selectedBatchId ? { background: "var(--accent-dim)" } : undefined}
                  onClick={() => { setSelectedBatchId(b.batch_id); navigate("/"); }}>
                <td className="mono">{b.batch_id}</td>
                <td className="mono">{b.dataset_version}</td>
                <td className="num">{formatNumber(b.total_cases)}</td>
                <td className="num">{formatNumber(b.resolved)}</td>
                <td className="num">{formatNumber(b.escalated)}</td>
                <td>{b.status}</td>
                <td className="mono" style={{ fontSize: 11 }}>{formatTimestamp(b.created_at)}</td>
              </tr>
            ))}
            {batches.length === 0 && <tr><td colSpan={7} className="empty-state">No batches yet — see Import Data.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <div className="panel-title" style={{ padding: "11px 14px 0" }}>Import jobs ({jobs.length})</div>
        <p className="page-sub" style={{ padding: "0 14px" }}>
          Every file upload you've started, including ones you navigated away from — a job's status survives a page
          reload because it's tracked here, not in the browser tab.
        </p>
        <table>
          <thead><tr><th>Job</th><th>Status</th><th className="num">Rows inserted</th><th>Batch</th><th>Updated</th><th></th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.job_id} className="clickable" onClick={() => navigate(`/import/${j.job_id}`)}>
                <td className="mono">{j.job_id.slice(0, 12)}…</td>
                <td><span className={`badge ${JOB_STATUS_TONE[j.status] ?? "neutral"}`}>{j.status}</span></td>
                <td className="num">{formatNumber(j.rows_inserted)}</td>
                <td className="mono">{j.batch_id ?? "—"}</td>
                <td className="mono" style={{ fontSize: 11 }}>{formatTimestamp(j.updated_at)}</td>
                <td><span className="link-btn">Open →</span></td>
              </tr>
            ))}
            {jobs.length === 0 && <tr><td colSpan={6} className="empty-state">No import jobs yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
