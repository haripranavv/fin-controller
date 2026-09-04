import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listImportJobs } from "../api";
import { useBatch } from "../context/BatchContext";
import { formatDuration, formatNumber, formatTimestamp } from "../format";
import type { ImportJobResponse } from "../types";

const JOB_STATUS_TONE: Record<string, string> = {
  QUEUED: "progress", VALIDATING: "progress", IMPORTING: "progress", READY: "resolved", FAILED: "escalated",
};

const TYPE_SHORT: Record<string, string> = {
  order: "Orders", payment: "Payments", refund: "Refunds",
  settlement: "Settlements", bank_transaction: "Bank", unknown: "Unrecognized", rejected_ground_truth: "Rejected",
};

function fileTypesOf(job: ImportJobResponse): string {
  const types = [...new Set(job.files.map((f) => TYPE_SHORT[f.detected_type] ?? f.detected_type))];
  return types.length ? types.join(", ") : "—";
}

function errorsOf(job: ImportJobResponse): number {
  return job.files.reduce((sum, f) => sum + f.invalid_row_count, 0);
}

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
          <thead>
            <tr>
              <th>Job</th><th>Status</th><th>File types</th><th className="num">Rows</th><th className="num">Errors</th>
              <th className="num">Elapsed</th><th>Batch</th><th>Updated</th><th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => {
              const errors = errorsOf(j);
              return (
                <tr key={j.job_id} className="clickable" onClick={() => navigate(`/import/${j.job_id}`)}>
                  <td className="mono">{j.job_id.slice(0, 10)}…</td>
                  <td><span className={`badge ${JOB_STATUS_TONE[j.status] ?? "neutral"}`} title={j.error_message ?? undefined}>{j.status}</span></td>
                  <td>{fileTypesOf(j)}</td>
                  <td className="num">{formatNumber(j.rows_inserted)} / {formatNumber(j.rows_total)}</td>
                  <td className="num" style={errors > 0 ? { color: "var(--escalated)" } : undefined}>{formatNumber(errors)}</td>
                  <td className="num">{formatDuration(j.elapsed_seconds)}</td>
                  <td className="mono">{j.batch_id ?? "—"}</td>
                  <td className="mono" style={{ fontSize: 11 }}>{formatTimestamp(j.updated_at)}</td>
                  <td><span className="link-btn">Open →</span></td>
                </tr>
              );
            })}
            {jobs.length === 0 && <tr><td colSpan={9} className="empty-state">No import jobs yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
