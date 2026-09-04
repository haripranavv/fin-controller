import { severityTone, stateTone } from "../format";

export function StateBadge({ state }: { state: string }) {
  return <span className={`badge ${stateTone(state)}`}>{state.replace(/_/g, " ")}</span>;
}

export function SeverityBadge({ severity }: { severity: string | null | undefined }) {
  if (!severity) return <span className="pill">—</span>;
  return <span className={`badge sev-${severityTone(severity)}`}>{severity}</span>;
}

export function ResolvedViaPill({ via }: { via: string | null | undefined }) {
  if (!via) return <span className="pill">—</span>;
  if (via === "ai") return <span className="pill ai">AI</span>;
  if (via === "deterministic") return <span className="pill">rule</span>;
  if (via === "clean") return <span className="pill">clean</span>;
  return <span className="pill">{via}</span>;
}
