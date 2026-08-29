import { useState } from "react";
import { resumeTask } from "../api";
import "./ResumePanel.css";

interface Props {
  taskId: string;
  currentBudget: number;
  // Fraction (0-1) of currentBudget already spent. The "add budget" field
  // only earns its place in this panel when the budget is actually close to
  // running out -- most stops (operator hit Stop, a transient network
  // error) have nothing to do with cost, so defaulting to "here's a budget
  // form" every time is the wrong prompt for the common case.
  budgetUsedFraction: number;
  escalationReason: string | null;
  onResumed: () => void;
  // "done" has no failure reason to restate and nothing to nudge with on its
  // own -- unlike escalated/stopped, the backend requires a message here (a
  // silent reopen would likely just re-run the same investigation into the
  // same premature conclusion). Distinct from an empty-optional-note case,
  // so the button/placeholder need to say so, not just rely on the backend
  // 400 to explain it after the fact.
  requireMessage?: boolean;
}

const BUDGET_FIELD_THRESHOLD = 0.75;

export function ResumePanel({ taskId, currentBudget, budgetUsedFraction, escalationReason, onResumed, requireMessage }: Props) {
  const [extraBudget, setExtraBudget] = useState(2.0);
  const [message, setMessage] = useState("");
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = message.trim();
  const blockedByMessage = requireMessage && trimmed.length === 0;
  const nearBudgetLimit = budgetUsedFraction >= BUDGET_FIELD_THRESHOLD;
  const addedBudget = nearBudgetLimit ? extraBudget : 0;

  async function handleResume() {
    setResuming(true);
    setError(null);
    try {
      await resumeTask(taskId, addedBudget, trimmed || undefined);
      onResumed();
    } catch (e) {
      setError(e instanceof Error ? e.message : "resume failed");
    } finally {
      setResuming(false);
    }
  }

  return (
    <div className="resume-panel">
      <div className="resume-panel-header">
        <strong>{requireMessage ? "Task marked done" : "Task stopped"}</strong> —{" "}
        {escalationReason ?? "escalated for review"}
      </div>
      <p className="resume-panel-sub">
        The plan and everything done so far is preserved. Resuming re-plans from here
        {requireMessage ? " with your instructions below" : " (and your note, if any)"} — it doesn't just retry
        blindly.
      </p>
      {nearBudgetLimit && (
        <div className="resume-panel-row">
          <label className="resume-field">
            <span>Add budget (current: ${currentBudget.toFixed(2)}, nearly spent)</span>
            <input
              type="number"
              min={0.1}
              step={0.5}
              value={extraBudget}
              onChange={(e) => setExtraBudget(parseFloat(e.target.value) || 0)}
            />
          </label>
        </div>
      )}
      <textarea
        className="resume-message"
        placeholder={
          requireMessage
            ? "What should it do next? (required — e.g. \"keep investigating, that wasn't actually finished\")"
            : "Optional note for the resumed plan (e.g. what to prioritize, what went wrong)..."
        }
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        rows={2}
      />
      {error && <div className="resume-error">{error}</div>}
      <button
        className="resume-btn"
        onClick={handleResume}
        disabled={resuming || (nearBudgetLimit && extraBudget <= 0) || blockedByMessage}
      >
        {resuming ? "Resuming..." : nearBudgetLimit ? `Resume with $${(currentBudget + extraBudget).toFixed(2)} total budget` : "Resume"}
      </button>
    </div>
  );
}
