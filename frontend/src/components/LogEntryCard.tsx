import { useState } from "react";
import type { LogEntry } from "../types";
import "./LogEntryCard.css";

const NODE_CONFIG: Record<string, { label: string; color: string }> = {
  work: { label: "Work", color: "var(--blue)" },
  verify_and_ship: { label: "Verify", color: "var(--amber)" },
  operator: { label: "You", color: "var(--text)" },
};

// A subagent's own turns arrive tagged "work:<name>" (e.g. "work:investigator")
// -- distinguish them visually from the coordinator's own "work" entries
// without needing to know every subagent name in advance.
function nodeConfig(node: string): { label: string; color: string } {
  if (NODE_CONFIG[node]) return NODE_CONFIG[node];
  if (node.startsWith("work:")) {
    return { label: node.slice("work:".length), color: "#d264c9" };
  }
  return { label: node, color: "var(--text-dim)" };
}

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const s = Math.max(0, Math.floor(diffMs / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

export function LogEntryCard({ entry, index }: { entry: LogEntry; index: number }) {
  // Defaults to expanded — collapsed-by-default made the reasoning hard to
  // follow without clicking through every single entry (direct feedback:
  // "all of the agent text comes in a collapsed box"). Still collapsible
  // for anyone who wants to declutter a long-running task's feed.
  const [expanded, setExpanded] = useState(true);
  const cfg = nodeConfig(entry.node);
  const hasDetail = entry.detail && entry.detail.trim().length > 0;

  return (
    <div
      className="log-entry"
      style={{ borderLeftColor: cfg.color, animationDelay: `${Math.min(index, 8) * 30}ms` }}
    >
      <div className="log-entry-header" onClick={() => hasDetail && setExpanded((e) => !e)}>
        <span className="log-entry-node" style={{ color: cfg.color }}>
          {cfg.label}
        </span>
        <span className="log-entry-summary">{entry.summary}</span>
        <span className="log-entry-meta">
          {entry.cost_usd > 0 && <span className="log-entry-cost">${entry.cost_usd.toFixed(4)}</span>}
          <span className="log-entry-time">{relativeTime(entry.timestamp)}</span>
          {hasDetail && <span className={`log-entry-chevron ${expanded ? "open" : ""}`}>&#9656;</span>}
        </span>
      </div>
      {expanded && hasDetail && <div className="log-entry-detail">{entry.detail}</div>}
    </div>
  );
}
