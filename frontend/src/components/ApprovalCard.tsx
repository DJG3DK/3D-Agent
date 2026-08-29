import { useState } from "react";
import { approveTask } from "../api";
import type { PendingApproval } from "../types";
import { AutoGrowTextarea } from "./AutoGrowTextarea";
import "./ApprovalCard.css";

interface Props {
  taskId: string;
  pendingApproval: PendingApproval;
  onDecided: () => void;
}

// Renders deep_agent.py's INTERRUPT_ON payload verbatim (action_requests --
// see types.ts's own comment on why this stays a pass-through, not a
// reshaped view) and submits one decision applying to every pending
// action_request in this batch -- see server.py's approve_task endpoint
// docstring for why per-action-request decisions aren't supported yet.
export function ApprovalCard({ taskId, pendingApproval, onDecided }: Props) {
  const [rejectMessage, setRejectMessage] = useState("");
  const [showRejectInput, setShowRejectInput] = useState(false);
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ask_user interrupts are QUESTIONS, not sensitive-action approvals — the
  // operator's typed answer becomes the tool result (respond decision).
  const isQuestion = pendingApproval.action_requests.every((r) => r.name === "ask_user");

  async function submit(decision: "approve" | "reject" | "respond") {
    setSubmitting(true);
    setError(null);
    try {
      const message =
        decision === "reject" ? rejectMessage.trim() || undefined :
        decision === "respond" ? answer.trim() : undefined;
      await approveTask(taskId, decision, message);
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : "approval failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (isQuestion) {
    return (
      <div className="approval-card approval-card--question">
        <div className="approval-card-header">
          <strong>The agent has a question</strong> — it paused to check with you before proceeding
        </div>
        <div className="approval-card-requests">
          {pendingApproval.action_requests.map((req, i) => (
            <div className="approval-request" key={i}>
              <pre className="approval-request-description approval-question-text">
                {String(req.args?.question ?? req.description ?? "")}
                {req.args?.options ? `\n\nOptions:\n${String(req.args.options)}` : ""}
              </pre>
            </div>
          ))}
        </div>
        <AutoGrowTextarea
          className="approval-answer-input"
          ariaLabel="Your answer to the agent"
          placeholder="Type your answer — it goes straight to the agent as direction"
          value={answer}
          onChange={setAnswer}
          minHeight={72}
          maxHeight={360}
        />
        <div className="approval-card-actions">
          <button
            className="approval-answer-btn"
            disabled={submitting || !answer.trim()}
            onClick={() => submit("respond")}
          >
            Answer
          </button>
        </div>
        {error && <div className="approval-error">{error}</div>}
      </div>
    );
  }

  return (
    <div className="approval-card">
      <div className="approval-card-header">
        <strong>Approval needed</strong> — the agent wants to run something flagged as sensitive
      </div>
      <div className="approval-card-requests">
        {pendingApproval.action_requests.map((req, i) => (
          <div className="approval-request" key={i}>
            <div className="approval-request-name">{req.name}</div>
            {req.description && <pre className="approval-request-description">{req.description}</pre>}
            {!req.description && (
              <pre className="approval-request-description">{JSON.stringify(req.args, null, 2)}</pre>
            )}
          </div>
        ))}
      </div>

      {showRejectInput && (
        <AutoGrowTextarea
          className="approval-reject-message"
          ariaLabel="Why this is being rejected"
          placeholder="Optional: why is this being rejected? (sent back to the agent)"
          value={rejectMessage}
          onChange={setRejectMessage}
          minHeight={56}
          maxHeight={280}
        />
      )}
      {error && <div className="approval-error">{error}</div>}

      <div className="approval-card-actions">
        <button className="approval-btn approval-btn-approve" onClick={() => submit("approve")} disabled={submitting}>
          {submitting ? "Submitting..." : "Approve"}
        </button>
        {showRejectInput ? (
          <button
            className="approval-btn approval-btn-reject"
            onClick={() => submit("reject")}
            disabled={submitting}
          >
            {submitting ? "Submitting..." : "Confirm reject"}
          </button>
        ) : (
          <button className="approval-btn approval-btn-reject-outline" onClick={() => setShowRejectInput(true)} disabled={submitting}>
            Reject
          </button>
        )}
      </div>
    </div>
  );
}
