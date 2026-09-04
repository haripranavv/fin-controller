import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { openEventStream } from "../api";
import { useBatch } from "../context/BatchContext";
import { formatTime } from "../format";
import { narrateEvent } from "../narrate";
import type { AgentEventItem } from "../types";

const MAX_EVENTS = 400;

export default function AgentActivity() {
  const { selectedBatchId } = useBatch();
  const [events, setEvents] = useState<AgentEventItem[]>([]);
  const [connected, setConnected] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    setEvents([]);
    setError(null);
    if (!selectedBatchId) return;

    const source = openEventStream(selectedBatchId, 0);
    sourceRef.current = source;
    setStreaming(true);

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (msg) => {
      const item = JSON.parse(msg.data) as AgentEventItem;
      setEvents((prev) => [item, ...prev].slice(0, MAX_EVENTS));
    };
    source.addEventListener("done", () => {
      setStreaming(false);
      setConnected(false);
      source.close();
    });

    return () => { source.close(); sourceRef.current = null; };
  }, [selectedBatchId]);

  if (!selectedBatchId) return <div className="empty-state">No batch selected.</div>;

  return (
    <>
      <h1 className="page-title">Agent Activity</h1>
      <p className="page-sub">
        What the controller is doing, in plain language — {streaming
          ? (connected ? "live, watching for new events" : "connecting…")
          : `replayed from the persisted run (${events.length} events)`}.
        Technical state/tool names are shown alongside each step, not hidden.
      </p>

      {error && <div className="error-state">{error}</div>}

      <div className="panel" style={{ padding: 0 }}>
        <div className="event-log">
          {events.length === 0 && <div className="empty-state">No events yet for this batch.</div>}
          {events.map((e) => (
            <EventRow key={e.id} event={e} />
          ))}
        </div>
      </div>
    </>
  );
}

function EventRow({ event }: { event: AgentEventItem }) {
  const n = narrateEvent(event);
  const failed = event.verifier_result && event.verifier_result.passed === false;
  return (
    <div className={`activity-row tone-${n.tone}`}>
      <div className="activity-time mono">{formatTime(event.created_at)}</div>
      <div className="activity-body">
        <div className="activity-headline">
          {n.headline}
          {failed && <span className="badge escalated" style={{ marginLeft: 8 }}>verifier failed</span>}
        </div>
        {n.detail && <div className="activity-detail">{n.detail}</div>}
        <div className="activity-meta">
          <Link className="case-link mono" to={`/cases/${event.case_id}`}>{event.case_id}</Link>
          <span className="mono">{event.from_state ?? "(start)"} → {event.to_state}</span>
          {event.tool && <span className="pill">{event.tool}</span>}
        </div>
      </div>
    </div>
  );
}
