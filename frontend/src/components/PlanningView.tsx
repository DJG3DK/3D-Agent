import { useEffect, useRef, useState } from "react";
import { JumpToBottom } from "./JumpToBottom";
import type { AttachmentEntry } from "../api";
import { archivePlanningSession, createPlanningSession, uploadFiles } from "../api";
import type { PlanningLogEntry, PlanningSessionMeta } from "../types";
import { usePlanningStream } from "../usePlanningStream";
import { AutoGrowTextarea } from "./AutoGrowTextarea";
import { cleanText, ModelBadge, parseToolCalls, relativeTime, renderWithColorSwatches, TOOL_ICONS } from "./ChatMessage";
import "./ChatMessage.css";
import "./TaskView.css";
import "./NewTaskPanel.css";
import "./MessageInput.css";
import { StopButton } from "./StopButton";
import "./PlanningView.css";

interface Props {
  repos: string[];
  // null -- no session picked (or "+ New" in the sidebar) -- show the
  // start-a-session prompt instead of a conversation. Session list/
  // selection lives in App.tsx (mirroring how `selected: TaskMeta` works),
  // not here -- the sidebar needs the full list to render its own
  // Planning section regardless of which one (if any) is currently open.
  session: PlanningSessionMeta | null;
  // Same shape as App.tsx's handleCreate for a normal new task -- "Build
  // Now" hands the saved plan document off to the real build system exactly
  // the way a manually-typed goal would, no dedicated backend endpoint.
  onBuildNow: (goal: string, repo: string, budgetUsd: number) => void;
  onSessionCreated: (session: PlanningSessionMeta) => void;
}

function PlanningEntry({ entry }: { entry: PlanningLogEntry }) {
  const [open, setOpen] = useState(false);

  if (entry.kind === "user") {
    return (
      <div className="chat-row chat-row--user">
        <div className="chat-bubble chat-bubble--user">
          <div className="chat-text">{renderWithColorSwatches(entry.detail)}</div>
        </div>
        <span className="chat-time">{relativeTime(entry.timestamp)}</span>
      </div>
    );
  }

  if (entry.kind === "tool-result") {
    const body = entry.detail || entry.summary.slice("tool result:".length).trim();
    return (
      <div className="chat-tool-result">
        <div className="chat-tool-result-head" onClick={() => setOpen((o) => !o)}>
          <span className={`chat-chevron ${open ? "open" : ""}`}>▸</span>
          <span>output</span>
          <span className="chat-time">{relativeTime(entry.timestamp)}</span>
          <code className="chat-tool-result-preview">{body.replace(/\s+/g, " ").slice(0, 90)}</code>
        </div>
        {open && <pre className="chat-tool-result-body">{body}</pre>}
      </div>
    );
  }

  // "agent" -- either a tool call or prose, same distinction ChatMessage.tsx
  // makes from the summary prefix.
  if (entry.summary.startsWith("calling: ")) {
    const calls = parseToolCalls(entry.summary);
    return (
      <div className="chat-tools">
        <ModelBadge model={entry.model} />
        <span className="chat-time">{relativeTime(entry.timestamp)}</span>
        {calls.map((c, i) => (
          <span key={i} className="chat-tool-chip" title={c.args}>
            <span className="chat-tool-icon">{TOOL_ICONS[c.name] ?? "⚙"}</span>
            <span className="chat-tool-name">{c.name}</span>
            <span className="chat-tool-args">{c.args.slice(0, 64)}</span>
          </span>
        ))}
      </div>
    );
  }

  const text = cleanText(entry.detail || entry.summary);
  return (
    <div className="chat-row chat-row--agent">
      <div className="chat-avatar">✦</div>
      <div className="chat-agent-col">
        <div className="chat-agent-name">
          Agent
          <ModelBadge model={entry.model} />
          <span className="chat-time">{relativeTime(entry.timestamp)}</span>
        </div>
        <div className="chat-bubble chat-bubble--agent">
          <div className="chat-text">{renderWithColorSwatches(text)}</div>
        </div>
      </div>
    </div>
  );
}

function NewSessionPanel({ repos, onStart, starting }: { repos: string[]; onStart: (repo: string) => void; starting: boolean }) {
  // audit H-13: derive, don't mirror -- see NewTaskPanel for the full note.
  const [repo, setRepo] = useState("");
  const effectiveRepo = repo || repos[0] || "";
  return (
    <div className="planning-start-panel">
      <div className="planning-start-card">
        <h1>Plan a project</h1>
        <p className="planning-start-sub">
          Research, talk through design/UI/UX direction, and land on a concrete plan before anything gets
          built. It remembers what it learns about this project between sessions, and can look at your other
          projects too. When you're ready, hit "Build Now" to hand the plan straight to a real build task.
        </p>
        <label className="field">
          <span>Repo</span>
          <select value={effectiveRepo} onChange={(e) => setRepo(e.target.value)}>
            {repos.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <button className="submit-btn" disabled={!effectiveRepo || starting} onClick={() => onStart(effectiveRepo)}>
          {starting ? "Starting..." : "Start Planning Session"}
        </button>
      </div>
    </div>
  );
}

function BuildNowPanel({ onConfirm }: { onConfirm: (budgetUsd: number) => void }) {
  const [open, setOpen] = useState(false);
  const [budget, setBudget] = useState(2.0);
  if (!open) {
    return (
      <button className="planning-build-btn" onClick={() => setOpen(true)}>
        🚀 Build Now
      </button>
    );
  }
  return (
    <div className="planning-build-confirm">
      <label className="field">
        <span>Budget (USD)</span>
        <input type="number" min={0.1} step={0.1} value={budget} onChange={(e) => setBudget(parseFloat(e.target.value) || 0)} />
      </label>
      <button className="planning-build-confirm-btn" onClick={() => onConfirm(budget)}>
        Confirm &amp; Start Building
      </button>
      <button className="planning-build-cancel-btn" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </div>
  );
}

/* Why the last turn ended, read from the persisted session rather than the
   live stream. A stream event only reaches whoever is watching at that
   second; this is what an operator sees on returning to a session that
   stopped, which was previously nothing at all. */
function OutcomeBanner({ session }: { session: PlanningSessionMeta | null }) {
  const outcome = session?.last_outcome;
  if (!outcome || outcome === "completed") return null;
  const tone = outcome === "stopped" ? "info" : outcome === "budget" ? "warn" : "bad";
  const headline: Record<string, string> = {
    stopped: "You stopped this turn.",
    budget: "The per-turn budget ceiling was reached.",
    stalled: "The turn was ended after going silent.",
    error: "The turn failed.",
  };
  return (
    <div className={`planning-outcome planning-outcome--${tone}`}>
      <strong>{headline[outcome] ?? "The turn ended."}</strong>
      {session?.last_outcome_detail ? <span> {session.last_outcome_detail}</span> : null}
      {outcome !== "stopped" && (
        <span className="planning-outcome-hint">
          {" "}Everything it read is still in this session &mdash; ask it to continue rather than starting over.
        </span>
      )}
    </div>
  );
}

export function PlanningView({ repos, session, onBuildNow, onSessionCreated }: Props) {
  const [starting, setStarting] = useState(false);
  const [text, setText] = useState("");
  const [planOpen, setPlanOpen] = useState(true);
  const [archiving, setArchiving] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const stream = usePlanningStream(session?.session_id ?? null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [stream.log.length]);

  async function handleStart(chosenRepo: string) {
    setStarting(true);
    try {
      const { session_id } = await createPlanningSession(chosenRepo);
      onSessionCreated({
        session_id,
        repo: chosenRepo,
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        title: null,
        plan_markdown: null,
        cost_usd: 0,
      });
    } finally {
      setStarting(false);
    }
  }

  async function handleSend() {
    if (!text.trim() || stream.running || uploading) return;
    const goal = text.trim();
    let attachments: AttachmentEntry[] | undefined;
    if (files.length) {
      setUploading(true);
      setUploadError(null);
      try {
        attachments = await uploadFiles(session!.repo, files);
      } catch (err) {
        setUploadError(err instanceof Error ? err.message : "upload failed");
        setUploading(false);
        return;
      }
      setUploading(false);
    }
    setText("");
    setFiles([]);
    stream.sendMessage(goal, attachments);
  }

  async function handleNewPlan() {
    if (!session) return;
    setArchiving(true);
    try {
      // Closes out the current plan (still fully reachable, just out of the
      // sidebar's default active list) before starting the next one fresh
      // in the same project -- matches how a finished task settles out of
      // the way once it's done, not deleted.
      await archivePlanningSession(session.session_id);
      const { session_id } = await createPlanningSession(session.repo);
      onSessionCreated({
        session_id,
        repo: session.repo,
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        title: null,
        plan_markdown: null,
        cost_usd: 0,
        archived: false,
      });
    } finally {
      setArchiving(false);
    }
  }

  if (!session) {
    return <NewSessionPanel repos={repos} onStart={handleStart} starting={starting} />;
  }

  const repo = session.repo;

  return (
    <div className="planning-view">
      <div className="planning-view-header">
        <span className="planning-view-repo">{repo}</span>
        <span className="planning-view-title">Planning session</span>
        <span className="planning-view-cost" title="Total spend for this session (no cap)">
          ${stream.costUsd.toFixed(3)}
        </span>
        <div className="planning-view-actions">
          {/* Same placement as TaskView's Stop: in the header beside the status,
              not down in the scrolling log. The log scrolls away, and a control
              you have to hunt for during a long turn is a control you do not
              have. Still gated on `running` — there is nothing to stop on an
              idle session, and a permanently greyed button is just clutter. */}
          {stream.running && session?.session_id && (
            <StopButton sessionId={session.session_id} onStopped={() => {}} />
          )}
          {stream.planMarkdown && <span className="planning-plan-badge">plan drafted</span>}
          <button className="planning-plan-toggle" onClick={() => setPlanOpen((o) => !o)} disabled={!stream.planMarkdown}>
            {planOpen ? "Hide plan" : "Show plan"}
          </button>
          {stream.planMarkdown && (
            <BuildNowPanel onConfirm={(budgetUsd) => onBuildNow(stream.planMarkdown!, repo, budgetUsd)} />
          )}
          <button className="planning-new-plan-btn" disabled={archiving} onClick={handleNewPlan} title="Archive this plan and start a fresh one for the same project">
            {archiving ? "Archiving..." : "New Plan"}
          </button>
        </div>
      </div>

      <div className="planning-view-body">
        <div className="planning-view-log-wrap">
        <JumpToBottom containerRef={logContainerRef} />
        <div className="planning-view-log" ref={logContainerRef}>
          <div className="chat-thread">
            {stream.hydrateError && <div className="hydrate-error">{stream.hydrateError}</div>}
            {!stream.running && <OutcomeBanner session={session} />}
            {stream.log.length === 0 && !stream.running && (
              <div className="planning-empty-hint">
                Tell it what you want to build, or ask it to research something first -- it can search the
                web, browse real pages (with screenshots for design reference), read this project (or any
                of your other projects, for comparison), and remembers what it learns here for next time.
              </div>
            )}
            {stream.log.map((entry, i) => (
              <PlanningEntry key={i} entry={entry} />
            ))}
            {stream.running && (
              <div className="chat-typing">
                <span /><span /><span />
              </div>
            )}
            {stream.sendError && <div className="planning-send-error">{stream.sendError}</div>}
            <div ref={logEndRef} />
          </div>
        </div>
        </div>

        {planOpen && stream.planMarkdown && (
          <div className="planning-plan-panel">
            <div className="planning-plan-panel-head">Current plan draft</div>
            <pre className="planning-plan-markdown">{stream.planMarkdown}</pre>
          </div>
        )}
      </div>

      <div className="planning-attach-row">
        <input
          ref={fileInput}
          type="file"
          multiple
          accept=".png,.jpg,.jpeg,.webp,.gif,.pdf,.csv,.tsv,.txt,.json,.md,.xlsx,.xls"
          style={{ display: "none" }}
          onChange={(e) => {
            setFiles((prev) => [...prev, ...Array.from(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <button type="button" className="attach-btn" disabled={stream.running || uploading} onClick={() => fileInput.current?.click()}>
          📎 Attach
        </button>
        {uploadError && <span className="planning-send-error">{uploadError}</span>}
        {files.length > 0 && (
          <div className="attach-chips">
            {files.map((f, i) => (
              <span className="attach-chip" key={i}>
                {f.name}
                <button type="button" onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}>×</button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="message-input">
        <AutoGrowTextarea
          ariaLabel="Message the planning agent"
        minHeight={84}
        maxHeight={260}
          placeholder={stream.running ? "Thinking..." : "Ask a question, share a reference, or describe what you want... (Shift+Enter for a new line)"}
          value={text}
          disabled={stream.running}
          onChange={setText}
          onSubmit={handleSend}
        />
        <button onClick={handleSend} disabled={stream.running || uploading || !text.trim()}>
          {uploading ? "…" : stream.running ? "…" : "➤"}
        </button>
      </div>
    </div>
  );
}
