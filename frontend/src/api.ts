import type {
  BatchSummary, CaseDetail, CaseListResponse, ExceptionListResponse,
  InvestigationDetail, OverviewResponse, RunStatus, SessionResponse,
} from "./types";
import type { AuthResponse } from "./types";
import type { ImportJobResponse } from "./types";

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://127.0.0.1:8000";
const TOKEN_KEY = "fc_session_token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(token: string | null): void {
  try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
}

function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/** Every failure path here - a non-2xx response, or fetch() itself
 * throwing (network refused, CORS blocked, DNS failure - all surface as
 * "TypeError: Failed to fetch") - is turned into a real Error with the
 * actual request path in it, rather than losing that context in a
 * generic toast. */
async function request(path: string, init: RequestInit = {}): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    throw new Error(`Could not reach the API at ${BASE}${path} (${reason}). Is the backend running and is VITE_API_BASE set correctly?`);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body && typeof body === "object" && "detail" in body ? String((body as { detail: unknown }).detail) : await res.text().catch(() => "");
    throw new Error(detail || `${res.status} ${res.statusText} for ${path}`);
  }
  return res;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await request(path, { headers: authHeaders() });
  return res.json() as Promise<T>;
}

async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  const res = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return res.json() as Promise<T>;
}

// --- auth -----------------------------------------------------------------

export function register(email: string, password: string, displayName?: string): Promise<AuthResponse> {
  return postJSON("/api/auth/register", { email, password, display_name: displayName || undefined });
}

export function login(email: string, password: string): Promise<AuthResponse> {
  return postJSON("/api/auth/login", { email, password });
}

/** Logs straight into the seeded synthetic demo account
 * (operator@financecontroller.demo) - no credentials needed, no
 * registration ever required to see the product. */
export function demoLogin(): Promise<AuthResponse> {
  return postJSON("/api/auth/demo-login");
}

export async function checkSession(): Promise<SessionResponse> {
  const res = await fetch(`${BASE}/api/auth/session`, { headers: authHeaders() });
  if (!res.ok) return { valid: false, email: null, is_demo: false };
  return res.json();
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/api/auth/logout`, { method: "POST", headers: authHeaders() }).catch(() => {});
}

// --- import (server-side async job) ---------------------------------------

export async function createImportJob(files: File[]): Promise<ImportJobResponse> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const res = await request("/api/import/jobs", { method: "POST", headers: authHeaders(), body: form });
  return res.json();
}

export function getImportJob(jobId: string): Promise<ImportJobResponse> {
  return getJSON(`/api/import/jobs/${encodeURIComponent(jobId)}`);
}

export function listImportJobs(): Promise<ImportJobResponse[]> {
  return getJSON("/api/import/jobs");
}

export function confirmImportJob(jobId: string, datasetVersion: string): Promise<ImportJobResponse> {
  return postJSON(`/api/import/jobs/${encodeURIComponent(jobId)}/confirm`, { dataset_version: datasetVersion });
}

// --- read-mostly operator console API --------------------------------------

export function listBatches(): Promise<BatchSummary[]> {
  return getJSON("/api/batches");
}

export function getOverview(batchId?: string): Promise<OverviewResponse> {
  const qs = batchId ? `?batch_id=${encodeURIComponent(batchId)}` : "";
  return getJSON(`/api/overview${qs}`);
}

export function listCases(params: {
  batchId: string; state?: string; q?: string; limit?: number; offset?: number;
}): Promise<CaseListResponse> {
  const qs = new URLSearchParams({ batch_id: params.batchId });
  if (params.state) qs.set("state", params.state);
  if (params.q) qs.set("q", params.q);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  return getJSON(`/api/cases?${qs.toString()}`);
}

export function getCaseDetail(caseId: string): Promise<CaseDetail> {
  return getJSON(`/api/cases/${encodeURIComponent(caseId)}`);
}

export function getCaseInvestigation(caseId: string): Promise<InvestigationDetail> {
  return getJSON(`/api/cases/${encodeURIComponent(caseId)}/investigation`);
}

export function listExceptions(batchId: string): Promise<ExceptionListResponse> {
  return getJSON(`/api/exceptions?batch_id=${encodeURIComponent(batchId)}`);
}

export function getBatchEvents(batchId: string, afterId = 0): Promise<import("./types").AgentEventItem[]> {
  return getJSON(`/api/runs/${encodeURIComponent(batchId)}/events?after_id=${afterId}`);
}

export function getRunStatus(batchId: string): Promise<RunStatus> {
  return getJSON(`/api/runs/${encodeURIComponent(batchId)}/status`);
}

export async function triggerRun(datasetVersion: string): Promise<RunStatus> {
  const res = await request("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ dataset_version: datasetVersion }),
  });
  return res.json();
}

/** Native EventSource cannot set an Authorization header - the session
 * token is carried as a `?token=` query param instead, which
 * get_current_user (app/api/routes_auth.py) accepts as a fallback for
 * exactly this reason. */
export function openEventStream(batchId: string, afterId = 0): EventSource {
  const token = getToken() ?? "";
  return new EventSource(`${BASE}/api/runs/${encodeURIComponent(batchId)}/stream?after_id=${afterId}&token=${encodeURIComponent(token)}`);
}
