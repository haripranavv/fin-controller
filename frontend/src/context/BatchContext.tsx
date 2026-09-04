import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { listBatches, getRunStatus, triggerRun as apiTriggerRun } from "../api";
import type { BatchSummary, RunStatus } from "../types";

interface BatchContextValue {
  batches: BatchSummary[];
  selectedBatchId: string | null;
  setSelectedBatchId: (id: string) => void;
  refreshBatches: () => Promise<void>;
  runStatus: RunStatus | null;
  triggerRun: (datasetVersion: string) => Promise<void>;
  runError: string | null;
}

const BatchContext = createContext<BatchContextValue | null>(null);

export function BatchProvider({ children }: { children: ReactNode }) {
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const refreshBatches = useCallback(async () => {
    const rows = await listBatches();
    setBatches(rows);
    setSelectedBatchId((current) => current ?? rows[0]?.batch_id ?? null);
  }, []);

  useEffect(() => {
    refreshBatches().catch((e) => setRunError(String(e)));
  }, [refreshBatches]);

  // Poll run status for the selected batch while a run might be active -
  // cheap, and lets the whole console (not just Agent Activity) reflect
  // a run finishing (e.g. Overview's counts).
  useEffect(() => {
    if (!selectedBatchId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const status = await getRunStatus(selectedBatchId);
        if (!cancelled) setRunStatus(status);
      } catch {
        /* batch may not have a tracked run yet - not an error */
      }
    };
    poll();
    const interval = setInterval(poll, 2000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [selectedBatchId]);

  const triggerRun = useCallback(async (datasetVersion: string) => {
    setRunError(null);
    try {
      const status = await apiTriggerRun(datasetVersion);
      setRunStatus(status);
      setSelectedBatchId(status.batch_id);
      await refreshBatches();
    } catch (e) {
      setRunError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshBatches]);

  const value = useMemo(
    () => ({ batches, selectedBatchId, setSelectedBatchId, refreshBatches, runStatus, triggerRun, runError }),
    [batches, selectedBatchId, refreshBatches, runStatus, triggerRun, runError],
  );

  return <BatchContext.Provider value={value}>{children}</BatchContext.Provider>;
}

export function useBatch(): BatchContextValue {
  const ctx = useContext(BatchContext);
  if (!ctx) throw new Error("useBatch must be used within BatchProvider");
  return ctx;
}
