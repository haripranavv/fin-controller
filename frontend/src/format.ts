export function formatRupees(paisa: number | null | undefined): string {
  if (paisa === null || paisa === undefined) return "—";
  const rupees = paisa / 100;
  return rupees.toLocaleString("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });
}

export function formatPercent(fraction: number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined) return "—";
  return `${(fraction * 100).toFixed(digits)}%`;
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-IN");
}

export function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m === 0) return `${rem}s`;
  const h = Math.floor(m / 60);
  if (h === 0) return `${m}m ${rem}s`;
  return `${h}h ${m % 60}m`;
}

export function stateTone(state: string): "resolved" | "escalated" | "progress" | "neutral" {
  if (state === "RESOLVED") return "resolved";
  if (state === "ESCALATED") return "escalated";
  if (state === "INGESTED" || state === "IN_PROGRESS") return "neutral";
  return "progress";
}

export function severityTone(sev: string | null | undefined): "low" | "medium" | "high" | "critical" | "neutral" {
  if (sev === "low" || sev === "medium" || sev === "high" || sev === "critical") return sev;
  return "neutral";
}
