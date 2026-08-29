import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { confirm2FA, setup2FA } from "../api";
import "./LoginPage.css";
import "./SetupTotpPage.css";

interface Props {
  onDone: () => void;
}

export function SetupTotpPage({ onDone }: Props) {
  const [secret, setSecret] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const started = useRef(false);
  const [setupError, setSetupError] = useState<string | null>(null);

  // audit M-20: the setup call had no catch, so a failure left a MANDATORY gate
  // with no QR code and the `started` ref blocked any retry -- a hard dead end.
  // Now a failure surfaces an error and re-arms retry.
  async function beginSetup() {
    setSetupError(null);
    try {
      const { secret, uri } = await setup2FA();
      setSecret(secret);
      setQrDataUrl(await QRCode.toDataURL(uri, { margin: 1, width: 220 }));
    } catch (err) {
      started.current = false; // allow a retry
      setSetupError(err instanceof Error ? err.message : "Couldn't start 2FA setup. Please retry.");
    }
  }

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    beginSetup();
  }, []);

  async function handleConfirm() {
    if (!code.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const { recovery_codes } = await confirm2FA(code.trim());
      setRecoveryCodes(recovery_codes);
    } catch (err) {
      setError(err instanceof Error ? err.message : "invalid code");
    } finally {
      setSubmitting(false);
    }
  }

  if (setupError && !qrDataUrl) {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-badge">2FA setup</div>
          <h1>Couldn't start 2FA setup</h1>
          <p className="login-sub" role="alert">{setupError}</p>
          <button className="btn btn-primary btn-block" onClick={() => { started.current = true; beginSetup(); }}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (recoveryCodes) {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-badge">2FA enabled</div>
          <h1>Save your recovery codes</h1>
          <p className="login-sub">
            Each code works once, if you ever lose access to your authenticator app. Save these somewhere
            safe -- they won't be shown again.
          </p>
          <div className="recovery-codes-grid">
            {recoveryCodes.map((c) => (
              <code key={c}>{c}</code>
            ))}
          </div>
          <button className="btn btn-primary btn-block" onClick={onDone}>
            I've saved these -- continue
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-badge">Required</div>
        <h1>Set up two-factor authentication</h1>
        <p className="login-sub">
          Scan this with an authenticator app (Google Authenticator, Authy, 1Password, etc). This is
          required for the admin account before you can use anything else.
        </p>
        {qrDataUrl && (
          <div className="totp-qr-wrap">
            <img src={qrDataUrl} alt="2FA QR code" width={220} height={220} />
          </div>
        )}
        {secret && (
          <p className="totp-manual-secret">
            Can't scan it? Enter manually: <code>{secret}</code>
          </p>
        )}
        <div className="form-field">
          <span>Enter the 6-digit code to confirm</span>
          <input
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleConfirm()}
          />
        </div>
        {error && <div className="error-banner">{error}</div>}
        <button className="btn btn-primary btn-block" disabled={submitting || !code.trim()} onClick={handleConfirm}>
          {submitting ? "Confirming..." : "Confirm"}
        </button>
      </div>
    </div>
  );
}
