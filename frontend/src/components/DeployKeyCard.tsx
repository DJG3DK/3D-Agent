import { useEffect, useState } from "react";
import {
  deleteDeployKey,
  generateDeployKey,
  getDeployKey,
  installDeployKey,
  testDeployKey,
  type DeployKeyStatus,
} from "../api";
import "./DeployKeyCard.css";

/* Push access for one project.
 *
 * Why this exists at all: the agent itself never pushes — `git push` is on its
 * blocked-command list. After a merge is approved the review service pushes
 * from the project's live checkout, and that push is best-effort: if it can't
 * authenticate, the merge and deploy still succeed and the remote just stays
 * behind. That failure is quiet, so this panel makes it visible up front.
 *
 * Two ways in, and the generated one is offered first on purpose: the operator
 * never handles a private key at all, they just copy a public key out.
 */

type Mode = "idle" | "paste";

export function DeployKeyCard({ project }: { project: string }) {
  const [status, setStatus] = useState<DeployKeyStatus | null>(null);
  const [mode, setMode] = useState<Mode>("idle");
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<{ ok: boolean; detail: string } | null>(null);
  const [copied, setCopied] = useState(false);

  async function load() {
    try {
      setStatus(await getDeployKey(project));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load key status");
    }
  }
  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [project]);

  async function act(fn: () => Promise<DeployKeyStatus>) {
    setBusy(true);
    setError(null);
    setTest(null);
    try {
      setStatus(await fn());
      setMode("idle");
      setPasted("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    setBusy(true);
    setTest(null);
    try {
      setTest(await testDeployKey(project));
    } catch (e) {
      setError(e instanceof Error ? e.message : "test failed");
    } finally {
      setBusy(false);
    }
  }

  function copyPublicKey() {
    if (!status?.public_key) return;
    void navigator.clipboard.writeText(status.public_key).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }

  if (!status) {
    return <div className="dk-card"><p className="dk-muted">{error || "Loading push access…"}</p></div>;
  }

  const noRemote = !status.remote;
  const isHttps = status.remote_kind === "https";

  return (
    <div className="dk-card">
      <div className="dk-head">
        <div>
          <h5>Push access</h5>
          <p className="dk-muted">
            Lets merged commits reach <code>{status.remote || "…"}</code>.
          </p>
        </div>
        <span className={`dk-pill ${status.installed && status.configured ? "dk-pill--on" : ""}`}>
          {status.installed && status.configured ? "key installed" : "no key"}
        </span>
      </div>

      {noRemote && (
        <p className="dk-note">
          This repo has no <code>origin</code> remote, so nothing is pushed anywhere — merges
          land in your local checkout and deploy from there. Add a remote if you want commits
          to reach GitHub.
        </p>
      )}

      {isHttps && <p className="dk-warn">{status.detail}</p>}

      {!noRemote && !isHttps && (
        <>
          {status.installed ? (
            <div className="dk-installed">
              <div className="dk-row">
                <span className="dk-label">Fingerprint</span>
                <code className="dk-fp">{status.fingerprint}</code>
              </div>
              {status.public_key && (
                <>
                  <div className="dk-row dk-row--top">
                    <span className="dk-label">Public key</span>
                    <code className="dk-pub">{status.public_key}</code>
                  </div>
                  <p className="dk-help">
                    Add this on the remote: <strong>repo → Settings → Deploy keys → Add deploy
                    key</strong>, and tick <strong>Allow write access</strong> — without that box
                    the key can read but the push will be rejected.
                  </p>
                </>
              )}
              <div className="dk-actions">
                <button className="dk-btn" onClick={copyPublicKey} disabled={!status.public_key}>
                  {copied ? "Copied" : "Copy public key"}
                </button>
                <button className="dk-btn" onClick={() => void runTest()} disabled={busy}>
                  {busy ? "Testing…" : "Test connection"}
                </button>
                <button className="dk-btn dk-btn--danger" disabled={busy}
                        onClick={() => void act(() => deleteDeployKey(project))}>
                  Remove
                </button>
              </div>
            </div>
          ) : (
            <>
              <details className="dk-explainer">
                <summary>What is a deploy key, and where do I get one?</summary>
                <p>
                  A <strong>deploy key</strong> is an SSH key that grants access to
                  <em> one repository</em> instead of your whole account — so if it ever leaks,
                  the blast radius is that repo alone. It's the right credential for a machine
                  that pushes on your behalf.
                </p>
                <p>The quickest path is to let this page make one for you:</p>
                <ol>
                  <li>Click <strong>Generate key</strong> below.</li>
                  <li>Copy the public key it shows you.</li>
                  <li>
                    On GitHub, open the repo → <strong>Settings</strong> →
                    <strong> Deploy keys</strong> → <strong>Add deploy key</strong>. Give it a
                    title, paste the key, and tick <strong>Allow write access</strong>.
                  </li>
                  <li>Come back and hit <strong>Test connection</strong>.</li>
                </ol>
                <p>
                  If you already generated one on the server
                  (<code>ssh-keygen -t ed25519 -f ./mykey -N ""</code>), paste its
                  <em> private</em> half instead — the file <em>without</em> the
                  <code>.pub</code> extension. It must have no passphrase, since nothing can
                  type one during an unattended push.
                </p>
              </details>

              {mode === "idle" && (
                <div className="dk-actions">
                  <button className="dk-btn dk-btn--primary" disabled={busy}
                          onClick={() => void act(() => generateDeployKey(project))}>
                    {busy ? "Generating…" : "Generate key"}
                  </button>
                  <button className="dk-btn" onClick={() => setMode("paste")} disabled={busy}>
                    Paste an existing key
                  </button>
                </div>
              )}

              {mode === "paste" && (
                <div className="dk-paste">
                  <textarea
                    className="dk-textarea"
                    rows={7}
                    spellCheck={false}
                    placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----\n…\n-----END OPENSSH PRIVATE KEY-----"}
                    value={pasted}
                    onChange={(e) => setPasted(e.target.value)}
                  />
                  <p className="dk-help">
                    Stored on this server with <code>0600</code> permissions and used only by
                    this project's git. It is never shown again after saving.
                  </p>
                  <div className="dk-actions">
                    <button className="dk-btn dk-btn--primary" disabled={busy || !pasted.trim()}
                            onClick={() => void act(() => installDeployKey(project, pasted))}>
                      {busy ? "Saving…" : "Save key"}
                    </button>
                    <button className="dk-btn" onClick={() => { setMode("idle"); setPasted(""); }}>
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </>
      )}

      {test && (
        <p className={test.ok ? "dk-ok" : "dk-warn"}>
          {test.ok ? "✓ " : ""}{test.detail}
        </p>
      )}
      {error && <p className="dk-warn">{error}</p>}
    </div>
  );
}
