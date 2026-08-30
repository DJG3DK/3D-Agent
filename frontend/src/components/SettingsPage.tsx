import { useEffect, useState } from "react";
import { RuntimeLimitsPanel } from "./RuntimeLimitsPanel";
import { changePassword, setAutoApprove, setMergeReview, getTelegramSettings, setTelegramSettings, sendTelegramTest } from "../api";
import type { CurrentUser } from "../types";
import "./SettingsPage.css";
import { ApiKeysPanel } from "./ApiKeysPanel";
import { ProjectsPanel } from "./ProjectsPanel";

interface Props {
  user: CurrentUser;
  onUserChanged: (user: CurrentUser) => void;
}

export function SettingsPage({ user, onUserChanged }: Props) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pwSaving, setPwSaving] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);

  const [autoSaving, setAutoSaving] = useState(false);
  const [autoError, setAutoError] = useState<string | null>(null);
  // Turning auto mode ON asks for a second, explicit confirmation; turning it
  // back OFF is always allowed immediately. Friction belongs on the side that
  // removes a safety prompt, never on the side that restores one.
  const [confirmingAuto, setConfirmingAuto] = useState(false);

  async function handlePasswordSave(e: React.FormEvent) {
    e.preventDefault();
    setPwError(null);
    setPwDone(false);
    if (next !== confirm) {
      setPwError("The two new-password fields don't match.");
      return;
    }
    setPwSaving(true);
    try {
      await changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setPwDone(true);
    } catch (err) {
      setPwError(err instanceof Error ? err.message : "change password failed");
    } finally {
      setPwSaving(false);
    }
  }

  const [mergeSaving, setMergeSaving] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  async function applyMergeReview(value: boolean) {
    setMergeError(null);
    setMergeSaving(true);
    try {
      await setMergeReview(value);
      onUserChanged({ ...user, require_merge_review: value });
    } catch (err) {
      setMergeError(err instanceof Error ? err.message : "saving merge review failed");
    } finally {
      setMergeSaving(false);
    }
  }

  async function applyAuto(value: boolean) {
    setAutoError(null);
    setAutoSaving(true);
    try {
      await setAutoApprove(value);
      onUserChanged({ ...user, auto_approve_commands: value });
      setConfirmingAuto(false);
    } catch (err) {
      setAutoError(err instanceof Error ? err.message : "saving auto mode failed");
    } finally {
      setAutoSaving(false);
    }
  }

  return (
    <div className="settings-page">
      <h1 className="settings-title">Settings</h1>
      <p className="settings-account">
        Signed in as <strong>{user.email}</strong>
        <span className="settings-role">{user.role}</span>
      </p>

      

      {/* Grouped grid (2026-08-28): one center column stopped scaling past two
          cards. Cards now sit in themed sections, two-up on wide screens,
          stacking on narrow ones. */}
      <h3 className="settings-section-label">Account &amp; access</h3>
      <div className="settings-grid">
        <section className="settings-card">
          <h2>Change password</h2>
          <form onSubmit={handlePasswordSave} className="settings-form">
            <label className="field">
              <span>Current password</span>
              <input
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                required
              />
            </label>
            <label className="field">
              <span>New password</span>
              <input
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                required
              />
            </label>
            <label className="field">
              <span>Confirm new password</span>
              <input
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                required
              />
            </label>
            <p className="settings-hint">
              At least 12 characters, with an uppercase letter, a lowercase letter, and a digit.
            </p>
            {pwError && <div className="settings-error">{pwError}</div>}
            {pwDone && <div className="settings-ok">Password updated.</div>}
            <button className="submit-btn" type="submit" disabled={pwSaving || !current || !next || !confirm}>
              {pwSaving ? "Saving…" : "Update password"}
            </button>
          </form>
        </section>
      </div>

      <h3 className="settings-section-label">Agent behavior</h3>
      <div className="settings-grid">
        <section className={`settings-card ${user.auto_approve_commands ? "settings-card--armed" : ""}`}>
          <h2>
            Auto mode
            <span className={`settings-pill ${user.auto_approve_commands ? "settings-pill--on" : ""}`}>
              {user.auto_approve_commands ? "ON" : "OFF"}
            </span>
          </h2>
          <p className="settings-body">
            Normally the agent pauses and asks before it touches something sensitive — a config file,
            a <code>.env</code>, anything under <code>.git/</code> or <code>.github/workflows</code>.
            That prompt is what makes a long task need babysitting. Auto mode runs those without
            asking, so a task can finish unattended.
          </p>
          <div className="settings-note settings-note--keep">
            <strong>Still always asks, even with auto mode on:</strong>
            <ul>
              <li>
                Destructive commands — <code>rm -rf</code>, <code>git push</code>, <code>sudo</code>,{" "}
                <code>chmod -R</code>, <code>chown -R</code>, redirects to <code>/dev/</code>, fork bombs
              </li>
              <li>Questions the agent asks you directly, so it gets your real answer</li>
            </ul>
          </div>
          <div className="settings-note settings-note--warn">
            <strong>What you give up:</strong> the agent edits config, secrets-adjacent files, CI
            workflows and deploy config with no prompt. Everything still runs inside the sandboxed
            checkout, still goes through the review gate before merging, and is still capped by the
            task budget — but you won't see those calls until you read the log afterwards.
          </div>
          <p className="settings-body settings-body--dim">
            Applies to tasks you start from now on. A task already running keeps the setting it began
            with, so flipping this can't loosen the gate on something already in flight.
          </p>
          {autoError && <div className="settings-error">{autoError}</div>}
          {user.auto_approve_commands ? (
            <button className="settings-btn settings-btn--off" disabled={autoSaving} onClick={() => applyAuto(false)}>
              {autoSaving ? "Saving…" : "Turn auto mode off"}
            </button>
          ) : confirmingAuto ? (
            <div className="settings-confirm">
              <span>Run sensitive file and shell operations without asking?</span>
              <button className="settings-btn settings-btn--danger" disabled={autoSaving} onClick={() => applyAuto(true)}>
                {autoSaving ? "Saving…" : "Yes, turn it on"}
              </button>
              <button className="settings-btn" disabled={autoSaving} onClick={() => setConfirmingAuto(false)}>
                Cancel
              </button>
            </div>
          ) : (
            <button className="settings-btn" onClick={() => setConfirmingAuto(true)}>
              Turn auto mode on…
            </button>
          )}
        </section>
        <section className={`settings-card ${user.require_merge_review ? "" : "settings-card--armed"}`}>
          <h2>
            Final merge review
            <span className={`settings-pill ${user.require_merge_review ? "settings-pill--on" : ""}`}>
              {user.require_merge_review ? "ON" : "OFF"}
            </span>
          </h2>
          <p className="settings-body">
            With this on, a task that passes the independent review service <em>parks</em> instead of
            merging: the full diff slides out from the right, you read it, and nothing ships until you
            hit <strong>Approve &amp; merge</strong> — or send it back with notes for another round.
          </p>
          <div className="settings-note settings-note--warn">
            <strong>Turning it off</strong> restores fully hands-free shipping: review-approved
            commits merge and deploy on their own. The review service still gates every merge either
            way — this toggle is only about <em>your</em> final look.
          </div>
          <p className="settings-body settings-body--dim">
            Applies to tasks you start from now on. A task already running keeps the setting it began
            with.
          </p>
          {mergeError && <div className="settings-error">{mergeError}</div>}
          {user.require_merge_review ? (
            <button className="settings-btn settings-btn--danger" disabled={mergeSaving} onClick={() => applyMergeReview(false)}>
              {mergeSaving ? "Saving…" : "Turn final review off — merge without me"}
            </button>
          ) : (
            <button className="settings-btn" disabled={mergeSaving} onClick={() => applyMergeReview(true)}>
              {mergeSaving ? "Saving…" : "Turn final review on"}
            </button>
          )}
        </section>
      </div>

      {user.role === "admin" && (
        <>
          <h3 className="settings-section-label">Runtime limits</h3>
          <p className="settings-section-hint">
            How hard the agent tries before giving up. Changes apply to the next turn or task &mdash;
            anything already running keeps the limits it started with. Hover a label for what it does.
          </p>
          <div className="settings-grid settings-grid--wide">
            <RuntimeLimitsPanel />
          </div>
        </>
      )}

      <h3 className="settings-section-label">Notifications</h3>
      <div className="settings-grid">
        <TelegramCard />
      </div>

      {/* Admin only, and gated server-side too — require_admin on both the
          read and the write. The client gate is convenience, not the
          boundary. The panel renders its own section label + one card per
          credential group. */}
      {user.role === "admin" && <ProjectsPanel />}

      {user.role === "admin" && <ApiKeysPanel />}
    

      
    </div>
  );
}


/** Telegram alert settings — bot token + chat id, saved per user. The token
 * is write-only: the backend never returns it (masked endpoint), so an
 * already-configured card shows a placeholder and sends the __unchanged__
 * sentinel unless the operator types a new one. */
function TelegramCard() {
  const [loaded, setLoaded] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTelegramSettings()
      .then((s) => {
        setConfigured(s.configured);
        setChatId(s.chat_id ?? "");
        setLoaded(true);
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : "failed to load telegram settings");
        setLoaded(true);
      });
  }, []);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setMsg(null);
    setSaving(true);
    try {
      const sendToken = token.trim() === "" && configured ? "__unchanged__" : token.trim();
      const s = await setTelegramSettings(sendToken, chatId.trim());
      setConfigured(s.configured);
      setToken("");
      setMsg(s.configured ? "Saved — use Send test to verify delivery." : "Cleared — alerts are off.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    setError(null);
    setMsg(null);
    setTesting(true);
    try {
      await sendTelegramTest();
      setMsg("Test message sent — check Telegram.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "test send failed");
    } finally {
      setTesting(false);
    }
  }

  return (
    <section className={`settings-card ${configured ? "settings-card--armed" : ""}`}>
      <h2>
        Telegram alerts
        <span className={`settings-pill ${configured ? "settings-pill--on" : ""}`}>
          {configured ? "ON" : "OFF"}
        </span>
      </h2>
      <p className="settings-hint">
        Pushes the moments that need you to Telegram: task escalations, approval and merge-review
        stops, completions, and failures — each with details and cost so far. Create a bot with
        @BotFather, message it once, then get your chat id from @userinfobot.
      </p>
      {loaded && (
        <form onSubmit={handleSave} className="settings-form">
          <label className="field">
            <span>Bot token</span>
            <input
              type="password"
              autoComplete="off"
              placeholder={configured ? "•••••• (saved — leave blank to keep)" : "123456:ABC-DEF…"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Chat ID</span>
            <input
              type="text"
              autoComplete="off"
              placeholder="e.g. 123456789"
              value={chatId}
              onChange={(e) => setChatId(e.target.value)}
            />
          </label>
          <div className="settings-actions">
            <button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</button>
            <button type="button" disabled={testing || !configured} onClick={handleTest}>
              {testing ? "Sending…" : "Send test"}
            </button>
          </div>
          {msg && <p className="settings-done">{msg}</p>}
          {error && <p className="settings-error">{error}</p>}
        </form>
      )}
    </section>
  );
}
