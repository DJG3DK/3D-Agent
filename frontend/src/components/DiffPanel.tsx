import { useCallback, useEffect, useRef, useState } from "react";
import { getTaskDiff, submitMergeDecision } from "../api";
import type { TaskDiff, TaskDiffFile } from "../types";
import "./DiffPanel.css";

/**
 * Slide-out diff viewer, right edge. Two modes, one component:
 *
 *  - LIVE (task running): polls the workspace diff every few seconds so the
 *    operator can watch the agent's edits land file by file. Read-only.
 *  - FINAL LOOK (status awaiting_merge): same rendering, plus the decision
 *    footer -- Approve & merge, or Send back with notes. The panel opens
 *    itself when a task parks on awaiting_merge; the operator can also open
 *    it any time from the Changes button.
 *
 * Rendering is line-shading over the raw unified diff -- additions green,
 * deletions red, hunk headers dimmed -- rather than a side-by-side widget.
 * That keeps the payload small, works for every file git can diff, and reads
 * the way the operator already reads diffs everywhere else.
 */

const LIVE_POLL_MS = 4000;

interface Props {
  taskId: string;
  open: boolean;
  onClose: () => void;
  /** Task is actively running -> poll; parked on awaiting_merge -> decide. */
  live: boolean;
  awaitingMerge: boolean;
  onDecided?: () => void;
}

function DiffLine({ line }: { line: string }) {
  let cls = "diff-line";
  if (line.startsWith("+") && !line.startsWith("+++")) cls += " diff-add";
  else if (line.startsWith("-") && !line.startsWith("---")) cls += " diff-del";
  else if (line.startsWith("@@")) cls += " diff-hunk";
  else if (line.startsWith("diff --git") || line.startsWith("index ")
        || line.startsWith("+++") || line.startsWith("---")) cls += " diff-meta";
  return <div className={cls}>{line || " "}</div>;
}

function FileDiff({ file }: { file: TaskDiffFile }) {
  // Collapsed by default beyond a handful of files? No -- the operator asked
  // for a final look; hiding the content behind clicks defeats it. Only very
  // large patches (server-side truncated) and binaries summarize.
  const [open, setOpen] = useState(true);
  return (
    <div className="diff-file">
      <button className="diff-file-header" onClick={() => setOpen((o) => !o)}>
        <span className="diff-file-path">{file.path}</span>
        <span className="diff-file-stats">
          {file.untracked && <span className="diff-badge-new">new</span>}
          {file.binary && <span className="diff-badge-bin">binary</span>}
          {file.additions != null && <span className="diff-stat-add">+{file.additions}</span>}
          {file.deletions != null && <span className="diff-stat-del">−{file.deletions}</span>}
          <span className="diff-caret">{open ? "▾" : "▸"}</span>
        </span>
      </button>
      {open && (
        <div className="diff-file-body">
          {file.binary ? (
            <div className="diff-elided">Binary file — no text diff.</div>
          ) : file.truncated ? (
            <div className="diff-elided">
              Patch too large to display inline ({file.additions ?? "?"} additions) — review this one in the repo.
            </div>
          ) : (
            file.patch.split("\n").map((l, i) => <DiffLine key={i} line={l} />)
          )}
        </div>
      )}
    </div>
  );
}

export function DiffPanel({ taskId, open, onClose, live, awaitingMerge, onDecided }: Props) {
  const [diff, setDiff] = useState<TaskDiff | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [sendingBack, setSendingBack] = useState(false);
  const [deciding, setDeciding] = useState<null | "approve" | "request_changes">(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setDiff(await getTaskDiff(taskId));
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "failed to load diff");
    }
  }, [taskId]);

  useEffect(() => {
    if (!open) return;
    void refresh();
    // Live mode keeps polling so the shading tracks the agent's edits in
    // near-real time; the final look is a settled commit, one fetch is right.
    if (live) {
      timerRef.current = window.setInterval(() => void refresh(), LIVE_POLL_MS);
      return () => { if (timerRef.current) window.clearInterval(timerRef.current); };
    }
  }, [open, live, refresh]);

  async function decide(decision: "approve" | "request_changes") {
    if (decision === "request_changes" && !notes.trim()) return;
    if (decision === "approve"
        && !confirm("Approve and merge this diff into live? The merge runs immediately.")) return;
    setDeciding(decision);
    setDecisionError(null);
    try {
      await submitMergeDecision(taskId, decision, notes.trim() || undefined);
      setNotes("");
      setSendingBack(false);
      onDecided?.();
      onClose();
    } catch (e) {
      setDecisionError(e instanceof Error ? e.message : "decision failed");
    } finally {
      setDeciding(null);
    }
  }

  return (
    <>
      {open && <div className="diff-panel-scrim" onClick={onClose} />}
      <aside className={`diff-panel ${open ? "diff-panel--open" : ""}`} aria-hidden={!open} inert={!open || undefined}>
        <div className="diff-panel-header">
          <div className="diff-panel-title">
            {awaitingMerge ? "Final look — approve to merge" : live ? "Live changes" : "Changes"}
            {live && <span className="diff-live-dot" title="polling the workspace" />}
          </div>
          <div className="diff-panel-totals">
            {diff && (
              <>
                <span className="diff-stat-add">+{diff.total_additions}</span>
                <span className="diff-stat-del">−{diff.total_deletions}</span>
                <span className="diff-file-count">{diff.files.length} file{diff.files.length === 1 ? "" : "s"}</span>
              </>
            )}
          </div>
          <button className="diff-panel-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="diff-panel-body">
          {loadError && <div className="diff-error">{loadError}</div>}
          {!diff && !loadError && <div className="diff-elided">Loading…</div>}
          {diff && diff.files.length === 0 && (
            <div className="diff-elided">No changes against {diff.base.slice(0, 10)} yet.</div>
          )}
          {diff?.files.map((f) => <FileDiff key={f.path} file={f} />)}
        </div>

        {awaitingMerge && (
          <div className="diff-panel-footer">
            {decisionError && <div className="diff-error">{decisionError}</div>}
            {sendingBack ? (
              <>
                <textarea
                  className="diff-notes"
                  autoFocus
                  placeholder="What should the agent change? Be specific — this text is its next instruction."
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
                <div className="diff-footer-row">
                  <button className="diff-btn-secondary" onClick={() => setSendingBack(false)} disabled={deciding !== null}>
                    Cancel
                  </button>
                  <button
                    className="diff-btn-warn"
                    onClick={() => void decide("request_changes")}
                    disabled={deciding !== null || !notes.trim()}
                  >
                    {deciding === "request_changes" ? "Sending…" : "Send back for more work"}
                  </button>
                </div>
              </>
            ) : (
              <div className="diff-footer-row">
                <button className="diff-btn-secondary" onClick={() => setSendingBack(true)} disabled={deciding !== null}>
                  Send back…
                </button>
                <button className="diff-btn-approve" onClick={() => void decide("approve")} disabled={deciding !== null}>
                  {deciding === "approve" ? "Merging…" : "Approve & merge"}
                </button>
              </div>
            )}
          </div>
        )}
      </aside>
    </>
  );
}
