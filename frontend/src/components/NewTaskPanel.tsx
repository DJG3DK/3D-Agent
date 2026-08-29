import { useRef, useState } from "react";
import "./NewTaskPanel.css";

interface Props {
  repos: string[];
  onSubmit: (goal: string, repo: string, budgetUsd: number, files: File[]) => void;
  submitting: boolean;
  error?: string | null;
  onClearError?: () => void;
}

export function NewTaskPanel({ repos, onSubmit, submitting, error, onClearError }: Props) {
  const [goal, setGoal] = useState("");
  // audit H-13: don't mirror repos[0] into state -- a useState initializer
  // never re-runs, so it captured "" (repos is [] at first mount, filled
  // async) and the Start Task button stayed dead while the dropdown showed
  // a repo. Derive the effective repo instead so the prop syncs in.
  const [repo, setRepo] = useState("");
  const effectiveRepo = repo || repos[0] || "";
  const [budget, setBudget] = useState(2.0);
  const [files, setFiles] = useState<File[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);

  return (
    <div className="new-task-panel">
      <div className="new-task-card">
        <h1>What should the agent build?</h1>
        <p className="new-task-sub">
          Plans, executes, and verifies against the real repo — then hands off to an independent review service before anything ships.
        </p>

        <textarea
          className="goal-input"
          placeholder="e.g. Add a helper function isValidEmail(value: string): boolean to apps/api/src/common, with a unit test."
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          rows={5}
          autoFocus
          spellCheck
          autoCorrect="on"
          autoCapitalize="sentences"
        />

        <div className="new-task-attach">
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
          <button type="button" className="attach-btn" onClick={() => fileInput.current?.click()}>
            📎 Attach files
          </button>
          <span className="attach-hint">screenshots, PDFs, CSVs — the agent can read them</span>
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

        <div className="new-task-row">
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
          <label className="field">
            <span>Budget (USD)</span>
            <input
              type="number"
              min={0.1}
              step={0.1}
              value={budget}
              onChange={(e) => setBudget(parseFloat(e.target.value) || 0)}
            />
          </label>
        </div>

        {error && (
          <div className="new-task-error" role="alert">
            {error}
            {onClearError && <button type="button" onClick={onClearError} aria-label="Dismiss error">×</button>}
          </div>
        )}
        <button
          className="submit-btn"
          disabled={!goal.trim() || !effectiveRepo || submitting}
          onClick={() => onSubmit(goal.trim(), effectiveRepo, budget, files)}
        >
          {submitting ? "Starting..." : "Start Task"}
        </button>
      </div>
    </div>
  );
}
