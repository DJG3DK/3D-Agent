import { useState } from "react";
import { forgotPassword, login, resetPassword, verify2FA } from "../api";
import type { CurrentUser } from "../types";
import "./LoginPage.css";

interface Props {
  onLoggedIn: (user: CurrentUser) => void;
  onBack: () => void;
}

type Mode = "login" | "2fa" | "forgot" | "reset" | "reset-done";

/* Shown under the card on every login view. This used to be a link to the
   source on GitHub, which was the useful thing to offer when the login screen
   WAS the front door. It is not any more -- the landing page in front of it
   carries that link (twice), so the job here is just a way back out. */
function LoginLinks({ onBack }: { onBack: () => void }) {
  return (
    <div className="login-links">
      <button type="button" onClick={onBack}>
        &lsaquo; Back to overview
      </button>
    </div>
  );
}

export function LoginPage({ onLoggedIn, onBack }: Props) {
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
        <LoginLinks onBack={onBack} />
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
        <LoginLinks onBack={onBack} />
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
        <LoginLinks onBack={onBack} />
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
        <LoginLinks onBack={onBack} />
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
      <LoginLinks onBack={onBack} />
    </div>
  );
}
