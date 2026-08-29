import { useState } from "react";
import { changePassword } from "../api";
import "./LoginPage.css";

interface Props {
  onDone: () => void;
}

export function ChangePasswordPage({ onDone }: Props) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    if (newPassword !== confirmPassword) {
      setError("new passwords don't match");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await changePassword(currentPassword, newPassword);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to change password");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-badge">Required</div>
        <h1>Set a new password</h1>
        <p className="login-sub">You're using a temporary password -- set a real one before continuing.</p>
        <div className="form-field">
          <span>Current (temporary) password</span>
          <input type="password" autoFocus value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} />
        </div>
        <div className="form-field">
          <span>New password</span>
          <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
        </div>
        <div className="form-field">
          <span>Confirm new password</span>
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
          />
        </div>
        <p className="login-sub" style={{ marginTop: -8, fontSize: 12 }}>
          At least 12 characters, with an uppercase letter, a lowercase letter, and a digit.
        </p>
        {error && <div className="error-banner">{error}</div>}
        <button
          className="btn btn-primary btn-block"
          disabled={submitting || !currentPassword || !newPassword || !confirmPassword}
          onClick={handleSubmit}
        >
          {submitting ? "Saving..." : "Set password"}
        </button>
      </div>
    </div>
  );
}
