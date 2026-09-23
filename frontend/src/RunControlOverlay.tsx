import { Download, Pause, Play, Square, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

const API = import.meta.env.VITE_API_URL || "";

type Run = {
  id: number;
  status: string;
  created_at: string;
  completed_at?: string | null;
  bidder_count: number;
  source_count: number;
  message?: string | null;
};

type RunSummary = {
  expected_tasks: number;
  counts: {
    completed: number;
    no_match: number;
    ambiguous: number;
    partial: number;
    blocked: number;
    failed: number;
    not_checked: number;
  };
};

const DISPLAY_STATUSES = new Set([
  "planned",
  "running",
  "pause_requested",
  "paused",
  "stop_requested",
  "cancelled",
]);

function titleForStatus(status: string) {
  switch (status) {
    case "planned": return "Starting research";
    case "running": return "Research running";
    case "pause_requested": return "Pausing research";
    case "paused": return "Research paused";
    case "stop_requested": return "Stopping research";
    case "cancelled": return "Research stopped";
    default: return status.replaceAll("_", " ");
  }
}

async function readJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep fallback.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export default function RunControlOverlay() {
  const [run, setRun] = useState<Run | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState("");
  const [hiddenRunId, setHiddenRunId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const runs = await readJson<{ items: Run[] }>("/api/runs");
      const latest = runs.items[0] || null;
      if (!latest || !DISPLAY_STATUSES.has(latest.status) || latest.id === hiddenRunId) {
        setRun(null);
        setSummary(null);
        return;
      }
      setRun(latest);
      try {
        const data = await readJson<{ item: RunSummary }>(`/api/runs/${latest.id}/summary`);
        setSummary(data.item);
      } catch {
        setSummary(null);
      }
    } catch {
      // This overlay is supplemental; the main app owns global API error handling.
    }
  }, [hiddenRunId]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const processed = useMemo(() => {
    if (!summary) return null;
    return Math.max(0, summary.expected_tasks - summary.counts.not_checked);
  }, [summary]);

  const control = async (action: "pause" | "stop" | "resume") => {
    if (!run) return;
    setActionBusy(true);
    setError("");
    try {
      const response = await readJson<{ item: Run }>(`/api/runs/${run.id}/${action}`, { method: "POST" });
      setRun(response.item);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Unable to ${action} run.`);
    } finally {
      setActionBusy(false);
    }
  };

  if (!run) return null;

  const canPause = run.status === "running" || run.status === "planned";
  const canResume = run.status === "paused";
  const canStop = !["stop_requested", "cancelled"].includes(run.status);
  const requestPending = ["pause_requested", "stop_requested"].includes(run.status);

  return (
    <aside className={`run-control-overlay run-control-${run.status}`}>
      <div className="run-control-heading">
        <div>
          <span>Run #{run.id}</span>
          <strong>{titleForStatus(run.status)}</strong>
        </div>
        {run.status === "cancelled" && (
          <button className="run-control-icon" title="Dismiss" onClick={() => setHiddenRunId(run.id)}><X size={16} /></button>
        )}
      </div>

      {summary && processed !== null && (
        <div className="run-control-progress">
          <div><span>Processed</span><strong>{processed} / {summary.expected_tasks}</strong></div>
          <div className="run-control-track"><i style={{ width: `${summary.expected_tasks ? Math.round((processed / summary.expected_tasks) * 100) : 0}%` }} /></div>
          <small>
            {summary.counts.blocked} blocked · {summary.counts.failed} failed · {summary.counts.partial} partial · {summary.counts.ambiguous} ambiguous
          </small>
        </div>
      )}

      <p>{run.message || "Research task state is being updated."}</p>
      {requestPending && <p className="run-control-note">Pause/stop takes effect after the source request currently in flight returns, so evidence is not left half-written.</p>}
      {error && <div className="run-control-error">{error}</div>}

      <div className="run-control-actions">
        {canPause && <button disabled={actionBusy} onClick={() => void control("pause")}><Pause size={14} /> Pause</button>}
        {canResume && <button disabled={actionBusy} onClick={() => void control("resume")}><Play size={14} /> Resume</button>}
        {canStop && <button className="danger" disabled={actionBusy} onClick={() => void control("stop")}><Square size={14} /> Stop</button>}
        <a href={`${API}/api/runs/${run.id}/diagnostics/export`}><Download size={14} /> Export JSON</a>
      </div>
    </aside>
  );
}
