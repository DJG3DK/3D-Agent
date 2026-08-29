import { useState } from "react";
import { forgotPassword, login, resetPassword, verify2FA } from "../api";
import type { CurrentUser } from "../types";
import "./LoginPage.css";

interface Props {
  onLoggedIn: (user: CurrentUser) => void;
}

type Mode = "login" | "2fa" | "forgot" | "reset" | "reset-done";

/* Shown under the card on every login view. Someone arriving from a shared
   link hits this page with no way in and, until now, nowhere else to go — the
   console is deliberately private, so the useful move is to point at what IS
   public: a link to the source. */
function LoginLinks() {
  return (
    <div className="login-links">
      <a href="https://github.com/DJG3DK/3D-Agent" target="_blank" rel="noopener noreferrer">
        <svg viewBox="0 0 16 16" aria-hidden="true" width="14" height="14" fill="currentColor">
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
        </svg>
        Source on GitHub
      </a>
    </div>
  );
}

export function LoginPage({ onLoggedIn }: Props) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tempToken, setTempToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [resetCode, setResetCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleLogin() {
    if (!email.trim() || !password) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await login(email.trim(), password);
      if (result.requires_2fa && result.temp_token) {
        setTempToken(result.temp_token);
        setMode("2fa");
      } else if (result.user) {
        onLoggedIn(result.user);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleVerify() {
    if (!tempToken || !code.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const { user } = await verify2FA(tempToken, code.trim());
      onLoggedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "invalid code");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSendResetCode() {
    if (!email.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await forgotPassword(email.trim());
      setMode("reset");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResetPassword() {
    if (!resetCode.trim() || !newPassword || !confirmPassword) return;
    if (newPassword !== confirmPassword) {
      setError("new passwords don't match");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await resetPassword(email.trim(), resetCode.trim(), newPassword);
      setMode("reset-done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "reset failed");
    } finally {
      setSubmitting(false);
    }
  }

  function backToSignIn() {
    setMode("login");
    setTempToken(null);
    setCode("");
    setResetCode("");
    setNewPassword("");
    setConfirmPassword("");
    setPassword("");
    setError(null);
  }

  if (mode === "2fa") {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-badge">3D-Agent</div>
          <h1>Two-factor code</h1>
          <p className="login-sub">Enter the 6-digit code from your authenticator app.</p>
          <label className="form-field">
            <span>Code</span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              maxLength={12}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleVerify()}
            />
          </label>
          {error && <div className="error-banner">{error}</div>}
          <button className="btn btn-primary btn-block" disabled={submitting || !code.trim()} onClick={handleVerify}>
            {submitting ? "Verifying..." : "Verify"}
          </button>
          <button className="login-back-link" onClick={backToSignIn}>
            ‹ Back to sign in
          </button>
        </div>
        <LoginLinks />
      </div>
    );
  }

  if (mode === "forgot") {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-badge">3D-Agent</div>
          <h1>Reset your password</h1>
          <p className="login-sub">Enter your email and we'll send a reset code.</p>
          <label className="form-field">
            <span>Email</span>
            <input
              type="email"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSendResetCode()}
            />
          </label>
          {error && <div className="error-banner">{error}</div>}
          <button className="btn btn-primary btn-block" disabled={submitting || !email.trim()} onClick={handleSendResetCode}>
            {submitting ? "Sending..." : "Send reset code"}
          </button>
          <button className="login-back-link" onClick={backToSignIn}>
            ‹ Back to sign in
          </button>
        </div>
        <LoginLinks />
      </div>
    );
  }

  if (mode === "reset") {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-badge">3D-Agent</div>
          <h1>Check your email</h1>
          <p className="login-sub">
            If {email} has an account, a 6-digit reset code is on its way -- it expires in 30 minutes.
          </p>
          <label className="form-field">
            <span>Reset code</span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              maxLength={6}
              value={resetCode}
              onChange={(e) => setResetCode(e.target.value)}
            />
          </label>
          <label className="form-field">
            <span>New password</span>
            <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
          </label>
          <label className="form-field">
            <span>Confirm new password</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleResetPassword()}
            />
          </label>
          <p className="login-sub" style={{ marginTop: -8, fontSize: 12 }}>
            At least 12 characters, with an uppercase letter, a lowercase letter, and a digit.
          </p>
          {error && <div className="error-banner">{error}</div>}
          <button
            className="btn btn-primary btn-block"
            disabled={submitting || !resetCode.trim() || !newPassword || !confirmPassword}
            onClick={handleResetPassword}
          >
            {submitting ? "Resetting..." : "Reset password"}
          </button>
          <button className="login-back-link" onClick={backToSignIn}>
            ‹ Back to sign in
          </button>
        </div>
        <LoginLinks />
      </div>
    );
  }

  if (mode === "reset-done") {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-badge">3D-Agent</div>
          <h1>Password reset</h1>
          <p className="login-sub">Your password has been changed. Sign in with it below.</p>
          <button className="btn btn-primary btn-block" onClick={backToSignIn}>
            Back to sign in
          </button>
        </div>
        <LoginLinks />
      </div>
    );
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-badge">3D-Agent</div>
        <h1>Sign in</h1>
        <label className="form-field">
          <span>Email</span>
          <input
            type="email"
            autoFocus
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleLogin()}
          />
        </label>
        <label className="form-field">
          <span>Password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleLogin()}
          />
        </label>
        {error && <div className="error-banner">{error}</div>}
        <button className="btn btn-primary btn-block" disabled={submitting || !email.trim() || !password} onClick={handleLogin}>
          {submitting ? "Signing in..." : "Sign in"}
        </button>
        <button
          className="login-back-link"
          onClick={() => {
            setError(null);
            setMode("forgot");
          }}
        >
          Forgot password?
        </button>
      </div>
      <LoginLinks />
    </div>
  );
}
