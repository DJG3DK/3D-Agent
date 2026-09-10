import { useCallback, useEffect, useMemo, useState } from "react";
import { actOnGitHubItem, getGitHubInbox, pollGitHubNow, type GitHubInboxItem, type GitHubItemState } from "../api";
import "./GitHubInboxView.css";

/* The GitHub inbox: everything the poller found, what policy decided, and
 * the buttons to decide the rest.
 *
 * "Active" is the default filter: proposed items waiting on a click, tasks
 * already started, snoozed ones, and items seen under an Off policy (listed
 * so the operator can see what turning a source on would do). Dismissed
 * and resolved items are one toggle away, never deleted, so a dismissed
 * PR does not come back until it actually changes.
 */

const KIND_LABEL: Record<GitHubInboxItem["kind"], string> = {
  dependabot_prs: "Dependabot PR",
  security_alerts: "Security alert",
  review_requests: "Changes requested",
  ci_failures: "Failing check",
};

const STATE_LABEL: Record<GitHubItemState, string> = {
  seen: "seen (source off)",
  proposed: "waiting for you",
  task_created: "task started",
  dismissed: "dismissed",
  snoozed: "snoozed",
  resolved: "resolved on GitHub",
};

const ACTIVE: GitHubItemState[] = ["proposed", "task_created", "snoozed", "seen"];

function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function GitHubInboxView({ isAdmin, onOpenTask }: { isAdmin: boolean; onOpenTask?: (taskId: string, repo: string) => void }) {
  const [items, setItems] = useState<GitHubInboxItem[]>([]);
  const [lastPoll, setLastPoll] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await getGitHubInbox();
      setItems(d.items);
      setLastPoll(d.last_poll?.at ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load the inbox");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, [load]);

  const shown = useMemo(() => {
    const list = showAll ? items : items.filter((i) => ACTIVE.includes(i.state));
    const rank: Record<GitHubItemState, number> = { proposed: 0, task_created: 1, snoozed: 2, seen: 3, dismissed: 4, resolved: 5 };
    return [...list].sort((a, b) => rank[a.state] - rank[b.state] || b.updated_at - a.updated_at);
  }, [items, showAll]);

  const waiting = items.filter((i) => i.state === "proposed").length;

  async function act(item: GitHubInboxItem, action: "approve" | "dismiss" | "snooze", days?: number) {
    setBusy(item.key);
    setNotice(null);
    try {
      const res = await actOnGitHubItem(item.repo, item.key, action, days);
      setItems((list) => list.map((i) => (i.key === item.key && i.repo === item.repo ? res.item : i)));
      if (action === "approve" && res.task_id) setNotice(`Task ${res.task_id.slice(0, 8)} started on ${item.repo}.`);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "action failed");
    } finally {
      setBusy(null);
    }
  }

  async function pollNow() {
    setPolling(true);
    setNotice(null);
    try {
      const res = await pollGitHubNow();
      const n = res.results.reduce((a, r) => a + (r.found ?? 0), 0);
      setNotice(res.results.length ? `Polled ${res.results.length} project${res.results.length === 1 ? "" : "s"}: ${n} item${n === 1 ? "" : "s"} on GitHub.` : "No project has a source switched on. Enable one under Settings → GitHub.");
      await load();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "poll failed");
    } finally {
      setPolling(false);
    }
  }

  return (
    <div className="ghi">
      <div className="ghi-head">
        <div>
          <h2 className="ghi-title">GitHub inbox</h2>
          <p className="ghi-sub">
            {waiting > 0 ? `${waiting} item${waiting === 1 ? "" : "s"} waiting for your decision.` : "Nothing waiting on you."}
            {lastPoll ? ` Last poll ${ago(lastPoll)}.` : " Not polled since the last restart."}
          </p>
        </div>
        <div className="ghi-controls">
          <label className="ghi-toggle">
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
            show dismissed &amp; resolved
          </label>
          {isAdmin && (
            <button type="button" className="gh-btn" disabled={polling} onClick={pollNow}>{polling ? "Polling…" : "Poll now"}</button>
          )}
        </div>
      </div>
      {notice && <div className="ghi-notice">{notice}</div>}
      {error && <div className="settings-error">{error}</div>}
      {loaded && shown.length === 0 && !error && (
        <div className="ghi-empty">
          <p>Nothing here{showAll ? "" : " that is active"}.</p>
          <p className="ghi-empty-hint">
            Items appear once a project has a source switched on under Settings → GitHub and the poller has run.
            Propose sends you an approve link; Auto starts the task within the project's cap.
          </p>
        </div>
      )}
      <ul className="ghi-list">
        {shown.map((item) => {
          const actionable = item.state === "proposed" || item.state === "snoozed" || item.state === "seen";
          const isBusy = busy === item.key;
          return (
            <li key={`${item.repo}/${item.key}`} className={`ghi-item ghi-item--${item.state}`}>
              <div className="ghi-item-top">
                <span className={`ghi-kind ghi-kind--${item.kind}`}>{KIND_LABEL[item.kind]}</span>
                <span className="ghi-repo">{item.repo}</span>
                <span className={`ghi-state ghi-state--${item.state}`}>{STATE_LABEL[item.state]}</span>
                <span className="ghi-when" title={new Date(item.updated_at * 1000).toLocaleString()}>{ago(item.updated_at)}</span>
              </div>
              <a className="ghi-item-title" href={item.url || undefined} target="_blank" rel="noreferrer">
                {item.number ? `#${item.number} ` : ""}{item.title}
              </a>
              {item.summary && <div className="ghi-summary">{item.summary}</div>}
              {item.reason && <div className="ghi-reason">{item.reason}</div>}
              <div className="ghi-actions">
                {item.task_id && (
                  onOpenTask
                    ? <button type="button" className="gh-btn" onClick={() => onOpenTask(item.task_id!, item.repo)}>Open task {item.task_id.slice(0, 8)}</button>
                    : <span className="ghi-task">task {item.task_id.slice(0, 8)}</span>
                )}
                {actionable && (
                  <>
                    <button type="button" className="gh-btn gh-btn--go" disabled={isBusy} onClick={() => act(item, "approve")}>
                      {isBusy ? "Working…" : "Approve & start task"}
                    </button>
                    <button type="button" className="gh-btn" disabled={isBusy} onClick={() => act(item, "snooze", 1)}>Snooze 1 d</button>
                    <button type="button" className="gh-btn" disabled={isBusy} onClick={() => act(item, "snooze", 7)}>7 d</button>
                    <button type="button" className="gh-btn gh-btn--danger" disabled={isBusy} onClick={() => act(item, "dismiss")}>Dismiss</button>
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
