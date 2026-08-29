import { useEffect, useState } from "react";
import { getConsolidationStatus, type ConsolidationStatus } from "../api";
import "./ConsolidationStatusPanel.css";

/** Nightly memory-consolidation health.
 *
 *  Exists because a failed run was previously indistinguishable from a healthy
 *  one: the script printed a line to a log and exited 0, so a provider
 *  incompatibility silently skipped consolidation for months. The three states
 *  that matter are "ran and succeeded", "ran and failed", and "hasn't run" —
 *  the last one being the case a log tail can never show you.
 */
export function ConsolidationStatusPanel() {
  const [status, setStatus] = useState<ConsolidationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showLog, setShowLog] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const s = await getConsolidationStatus();
        if (!cancelled) { setStatus(s); setError(null); }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "failed to load");
      }
    }
    load();
    const t = setInterval(load, 300_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  if (error) return <div className="consol-panel consol-panel--err">Consolidation status: {error}</div>;
  if (!status) return <div className="consol-panel">Loading consolidation status…</div>;

  const neverRan = !status.ran_at;
  const failed = status.ok === false;
  const stale = status.stale && !neverRan;
  const tone = neverRan || failed ? "bad" : stale ? "warn" : "good";
  const headline = neverRan
    ? "Never run"
    : failed
      ? `Failed (exit ${status.exit_code})`
      : stale
        ? "Stale"
        : "Healthy";

  return (
    <div className={`consol-panel consol-panel--${tone}`}>
      <div className="consol-head">
        <span className="consol-dot" />
        <span className="consol-title">Memory consolidation</span>
        <span className="consol-headline">{headline}</span>
      </div>
      <div className="consol-meta">
        {neverRan ? (
          <>No run recorded. The nightly job writes a marker on every run — if this
            persists, the cron entry isn't firing.</>
        ) : (
          <>
            Last run {status.ran_at?.replace("T", " ").replace("Z", " UTC")}
            {typeof status.age_hours === "number" && <> · {status.age_hours}h ago</>}
            {stale && <> · expected nightly, so this has missed at least one</>}
            {failed && <> · memory was NOT updated; episodes stay unconsolidated until re-run</>}
          </>
        )}
      </div>
      {status.tail && (
        <>
          <button className="consol-toggle" onClick={() => setShowLog((v) => !v)}>
            {showLog ? "Hide log" : "Show log"}
          </button>
          {showLog && <pre className="consol-log">{status.tail}</pre>}
        </>
      )}
    </div>
  );
}
