import { useState } from "react";
import { stopTask, stopPlanningTurn } from "../api";
import "./StopButton.css";

interface Props {
  /** Exactly one of these. A planning session and a build task are different
   *  things with different endpoints, but the operator-facing control is the
   *  same, so they share one button rather than two near-identical ones. */
  taskId?: string;
  sessionId?: string;
  onStopped: () => void;
}

export function StopButton({ taskId, sessionId, onStopped }: Props) {
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isPlanning = !!sessionId;

  async function handleStop() {
    const message = isPlanning
      // A planning turn holds no subprocesses and ships nothing, so the warning
      // is materially gentler than a build task's — but the plan question is
      // the one an operator actually cares about, so answer it up front.
      ? "Stop this planning turn now? The in-flight reply is discarded. Any plan already saved is kept, and you can carry on with another message."
      : "Stop this task now? Whatever's in flight is killed immediately, not finished first. Everything done so far — and the plan — is preserved and can be resumed later.";
    if (!confirm(message)) return;
    setStopping(true);
    setError(null);
    try {
      // The backend doesn't return until the run has actually finished
      // tearing down (subprocess killed, real cost recorded) — not just
      // until cancellation was requested — so by the time this resolves,
      // it really has stopped.
      if (isPlanning) await stopPlanningTurn(sessionId!);
      else await stopTask(taskId!);
      onStopped();
    } catch (e) {
      setError(e instanceof Error ? e.message : "stop failed");
    } finally {
      setStopping(false);
    }
  }

  return (
    <span className="stop-button-wrap">
      <button className="stop-button" onClick={handleStop} disabled={stopping}>
        {stopping ? "Stopping…" : "Stop"}
      </button>
      {error && <span className="stop-error">{error}</span>}
    </span>
  );
}
