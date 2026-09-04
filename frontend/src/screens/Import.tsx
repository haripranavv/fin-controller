import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { confirmImportJob, createImportJob, getImportJob } from "../api";
import { useBatch } from "../context/BatchContext";
import { formatDuration, formatNumber } from "../format";
import type { FileDetectionResult, ImportJobResponse } from "../types";

const TYPE_LABELS: Record<string, string> = {
  order: "Orders", payment: "Payments", refund: "Refunds",
  settlement: "Settlements", bank_transaction: "Bank transactions",
  unknown: "Unrecognized", rejected_ground_truth: "Rejected — ground truth file",
};

const POLL_MS = 1200;

export default function Import() {
  const navigate = useNavigate();
  const { jobId: routeJobId } = useParams();
  const { refreshBatches, setSelectedBatchId } = useBatch();
  const [job, setJob] = useState<ImportJobResponse | null>(null);
  const [uploading, setUploading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [datasetVersion, setDatasetVersion] = useState("");
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) { window.clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  // The job is a real, server-side, Postgres-persisted state machine
  // (app.models.import_job.ImportJob) - loading it fresh by job_id on
  // every mount (including a hard reload, or coming back from Batches
  // via the URL) is what makes progress survive navigation, rather than
  // holding it only in this component's state.
  useEffect(() => {
    stopPolling();
    if (!routeJobId) { setJob(null); return; }
    let cancelled = false;
    const load = async () => {
      try {
        const j = await getImportJob(routeJobId);
        if (cancelled) return;
        setJob(j);
        setError(null);
        if (j.status === "IMPORTING") {
          pollRef.current = window.setInterval(async () => {
            const latest = await getImportJob(routeJobId).catch(() => null);
            if (!latest || cancelled) return;
            setJob(latest);
            if (latest.status === "READY" || latest.status === "FAILED") {
              stopPolling();
              if (latest.status === "READY") { refreshBatches(); }
            }
          }, POLL_MS);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    load();
    return () => { cancelled = true; stopPolling(); };
  }, [routeJobId, stopPolling, refreshBatches]);

  async function handleSelectFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const result = await createImportJob(Array.from(files));
      navigate(`/import/${result.job_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleCreateBatch() {
    if (!job || !datasetVersion.trim() || confirming) return;
    // The button is also disabled below whenever job.status !== "VALIDATING",
    // and the backend independently rejects a second confirm on the same
    // job with 409 - this local guard just avoids the round trip for the
    // common case (a double click before the first response lands).
    setConfirming(true);
    setError(null);
    try {
      const result = await confirmImportJob(job.job_id, datasetVersion.trim());
      setJob(result);
      if (result.status === "IMPORTING") {
        pollRef.current = window.setInterval(async () => {
          const latest = await getImportJob(job.job_id).catch(() => null);
          if (!latest) return;
          setJob(latest);
          if (latest.status === "READY" || latest.status === "FAILED") {
            stopPolling();
            if (latest.status === "READY") refreshBatches();
          }
        }, POLL_MS);
      } else if (result.status === "READY") {
        refreshBatches();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setConfirming(false);
    }
  }

  function handleRunController() {
    if (!job?.batch_id || !job.dataset_version) return;
    setSelectedBatchId(job.batch_id);
    navigate("/activity");
  }

  const step = job?.status ?? "select";

  return (
    <>
      <h1 className="page-title">Import Data</h1>
      <p className="page-sub">
        Select files → detect source type → preview → validate → create batch → run controller.
        Uploaded files go through the same reconciliation pipeline as every other batch — nothing is treated differently.
        Large imports run as a background job, so you can navigate away and come back without losing progress.
      </p>

      <div className="import-steps">
        <StepTag n={1} label="Select files" active={!job} done={!!job} />
        <StepTag n={2} label="Preview & validate" active={step === "VALIDATING"} done={step === "IMPORTING" || step === "READY" || step === "FAILED"} />
        <StepTag n={3} label="Create batch" active={step === "IMPORTING"} done={step === "READY"} />
        <StepTag n={4} label="Run controller" active={step === "READY"} done={false} />
      </div>

      {error && <div className="error-state">{error}</div>}

      {!job && (
        <div className="panel">
          <div className="panel-title">Select financial source files</div>
          <p className="page-sub" style={{ marginBottom: 12 }}>
            CSV files, one per record type — orders, payments, refunds, settlements, bank transactions.
            Column headers must match the existing schema field names (e.g. order_id, merchant_id, amount_paisa, status for orders).
            Source type is detected automatically from the columns present — no need to name the files a particular way.
          </p>
          <input
            ref={fileInputRef} type="file" multiple accept=".csv,text/csv" disabled={uploading}
            onChange={(e) => handleSelectFiles(e.target.files)}
          />
          {uploading && <div className="loading-state">Uploading and validating…</div>}
        </div>
      )}

      {job && (job.status === "VALIDATING" || job.status === "IMPORTING" || job.status === "FAILED") && (
        <>
          {job.files.map((f) => <FilePreviewCard key={f.filename} file={f} />)}

          {job.status === "VALIDATING" && (
            <div className="panel">
              <div className="panel-title">Review &amp; confirm</div>
              {!job.any_ready && (
                <div className="error-state">No file had any valid rows to import — fix the issues above and start a new import.</div>
              )}
              {job.any_ready && (
                <>
                  <label className="login-label" htmlFor="dataset-version">Batch name (dataset_version)</label>
                  <input
                    id="dataset-version" type="text" value={datasetVersion}
                    onChange={(e) => setDatasetVersion(e.target.value)}
                    placeholder="e.g. q1-merchant-batch"
                    style={{ display: "block", width: 280, marginTop: 4, marginBottom: 12 }}
                  />
                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="primary" disabled={!datasetVersion.trim() || confirming} onClick={handleCreateBatch}>
                      {confirming ? "Creating batch…" : "Create batch"}
                    </button>
                    <button onClick={() => navigate("/import")}>Start over</button>
                  </div>
                </>
              )}
            </div>
          )}

          {job.status === "IMPORTING" && (
            <div className="panel">
              <div className="case-status-banner tone-progress">
                <div className="case-status-label">Inserting records into the database</div>
                <div className="case-status-sub">
                  {job.current_stage ?? "starting…"} · elapsed {formatDuration(job.elapsed_seconds)}
                  {job.rows_total > 0 && <> · {formatNumber(job.rows_inserted)} / {formatNumber(job.rows_total)} rows staged so far</>}
                </div>
              </div>
              <p className="page-sub" style={{ marginBottom: 0 }}>
                This page updates automatically — navigate away and come back any time, this job keeps running on the server
                and its progress is never lost. Large imports (hundreds of thousands of rows) can genuinely take a minute or more;
                the stage above only advances when a batch of rows has actually been inserted, it is never a simulated percentage.
              </p>
            </div>
          )}

          {job.status === "FAILED" && (
            <div className="panel">
              <div className="case-status-banner tone-escalated">
                <div className="case-status-label">Import failed</div>
                <div className="case-status-sub">{job.error_message ?? "Unknown error"}</div>
              </div>
              <button style={{ marginTop: 10 }} onClick={() => navigate("/import")}>Start a new import</button>
            </div>
          )}
        </>
      )}

      {job && job.status === "READY" && (
        <div className="panel">
          <div className="case-status-banner tone-resolved">
            <div className="case-status-label">Batch ready</div>
            <div className="case-status-sub">{job.batch_id} — {job.dataset_version} — {formatNumber(job.rows_inserted)} rows inserted</div>
          </div>
          <table style={{ marginTop: 12 }}>
            <thead><tr><th>Record type</th><th className="num">Valid rows</th></tr></thead>
            <tbody>
              {job.files.filter((f) => f.valid_row_count > 0).map((f) => (
                <tr key={f.filename}><td>{TYPE_LABELS[f.detected_type] ?? f.detected_type}</td><td className="num">{formatNumber(f.valid_row_count)}</td></tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
            <button className="primary" onClick={handleRunController}>Run controller →</button>
            <button onClick={() => navigate("/import")}>Import more files</button>
          </div>
        </div>
      )}
    </>
  );
}

function StepTag({ n, label, active, done }: { n: number; label: string; active: boolean; done: boolean }) {
  return (
    <div className={`import-step ${active ? "active" : ""} ${done ? "done" : ""}`}>
      <span className="import-step-n">{done ? "✓" : n}</span> {label}
    </div>
  );
}

function FilePreviewCard({ file }: { file: FileDetectionResult }) {
  const tone = file.ready ? "resolved" : "escalated";
  return (
    <div className="panel">
      <div className="import-file-header">
        <span className="mono" style={{ fontWeight: 600 }}>{file.filename}</span>
        <span className={`badge ${tone}`}>{TYPE_LABELS[file.detected_type] ?? file.detected_type}</span>
        <span className="pill">{formatNumber(file.row_count)} rows</span>
      </div>
      <div className="stat-row" style={{ marginTop: 8 }}>
        <div className="stat-block">
          <div className="stat-label">Valid rows</div>
          <div className="stat-value resolved" style={{ fontSize: 15 }}>{formatNumber(file.valid_row_count)}</div>
        </div>
        <div className="stat-block">
          <div className="stat-label">Invalid rows</div>
          <div className="stat-value escalated" style={{ fontSize: 15 }}>{formatNumber(file.invalid_row_count)}</div>
        </div>
        <div className="stat-block">
          <div className="stat-label">Missing fields</div>
          <div className="stat-value" style={{ fontSize: 15 }}>{formatNumber(file.missing_field_count)}</div>
        </div>
        <div className="stat-block">
          <div className="stat-label">Duplicates</div>
          <div className="stat-value" style={{ fontSize: 15 }}>{formatNumber(file.duplicate_count)}</div>
        </div>
      </div>

      {file.missing_required_columns.length > 0 && (
        <div className="error-state" style={{ marginTop: 10 }}>
          Missing required column(s): {file.missing_required_columns.join(", ")}
        </div>
      )}
      {file.detected_type === "rejected_ground_truth" && (
        <div className="error-state" style={{ marginTop: 10 }}>
          This file contains ground-truth fields and is refused — it will never be processed as source data.
        </div>
      )}
      {file.sample_errors.length > 0 && (
        <div className="import-sample-errors">
          {file.sample_errors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}

      {file.preview_rows.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 10 }}>
          <table>
            <thead><tr>{file.columns_found.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>
              {file.preview_rows.map((row, i) => (
                <tr key={i}>{file.columns_found.map((c) => <td key={c} className="mono">{row[c] ?? ""}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
