/**
 * WebAuthn gate for the public LiteLLM admin panel.
 *
 * The panel's outer wall used to be nginx basic auth - a shared secret,
 * phishable and brute-forceable in principle. This replaces it with the same
 * security model as the mail app: passkeys bound to this hostname, sessions
 * stored server-side as SHA-256 hashes, nothing to steal from a config dump.
 *
 * nginx consults GET /auth/check via auth_request for every panel request:
 *   - requests carrying "Authorization: Bearer ..." pass through untouched;
 *     LiteLLM validates its own tokens (the UI's API calls, and any caller
 *     holding the master key).
 *   - everything else needs the gate's session cookie, minted only by a
 *     passkey ceremony.
 *
 * Single operator, so state is one 0600 JSON file next to the service, and
 * enrolment follows the mail app's bootstrap-token pattern: a 0600 token file
 * with a 30-minute TTL, single-use, created at boot when no passkey exists
 * yet, or on demand with `node server.mjs issue-token`.
 *
 * Break-glass: keep a second vhost that skips the auth_request (here, one
 * reachable only over the tailnet) so a lost passkey never locks the
 * operator out.
 */

import { createHash, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, renameSync, statSync, unlinkSync, existsSync, chmodSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from '@simplewebauthn/server';

const ROOT = dirname(fileURLToPath(import.meta.url));
const STATE_PATH = join(ROOT, 'state', 'state.json');
const TOKEN_PATH = join(ROOT, 'state', 'enrolment-token');
const TOKEN_TTL_MS = 30 * 60_000;
const SESSION_TTL_MS = 30 * 24 * 60 * 60_000;
const CHALLENGE_TTL_MS = 5 * 60_000;

// The relying-party id and origin are the hostname passkeys get bound to.
// Deployment-specific, so they come from the environment (ecosystem.config.js
// lifts GATE_* out of ../.env) and there is deliberately no default to ship.
const RP_ID = process.env.GATE_RP_ID;
const ORIGIN = process.env.GATE_ORIGIN;
if (!RP_ID || !ORIGIN) {
  console.error('llm-auth-gate: GATE_RP_ID and GATE_ORIGIN must be set (see ../.env.example)');
  process.exit(1);
}
const HOST = '127.0.0.1';
const PORT = Number(process.env.GATE_PORT ?? 4010);
const COOKIE = 'llm_gate_session';

// ---------------------------------------------------------------- state ----

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE_PATH, 'utf8'));
  } catch {
    return { credentials: [], sessions: {} };
  }
}

function saveState(state) {
  // Prune expired sessions on every write so the file never grows unbounded.
  const now = Date.now();
  for (const [hash, session] of Object.entries(state.sessions)) {
    if (session.expiresAt < now) delete state.sessions[hash];
  }
  const tmp = `${STATE_PATH}.tmp`;
  writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 });
  renameSync(tmp, STATE_PATH);
}

const state = loadState();

// Ceremony challenges live in memory: single process, single operator.
const challenges = new Map(); // kind -> { challenge, expiresAt }

function putChallenge(kind, challenge) {
  challenges.set(kind, { challenge, expiresAt: Date.now() + CHALLENGE_TTL_MS });
}

function takeChallenge(kind) {
  const entry = challenges.get(kind);
  challenges.delete(kind);
  if (!entry || entry.expiresAt < Date.now()) return null;
  return entry.challenge;
}

// ------------------------------------------------------- enrolment token ----

function issueToken() {
  const token = randomBytes(32).toString('base64url');
  writeFileSync(TOKEN_PATH, token, { mode: 0o600 });
  chmodSync(TOKEN_PATH, 0o600);
  return token;
}

/** Valid only while the 0600 file exists, matches, and is younger than the TTL. */
function consumeToken(presented) {
  try {
    const age = Date.now() - statSync(TOKEN_PATH).mtimeMs;
    const stored = readFileSync(TOKEN_PATH, 'utf8').trim();
    if (age > TOKEN_TTL_MS) {
      unlinkSync(TOKEN_PATH);
      return false;
    }
    if (!presented || stored !== presented) return false;
    unlinkSync(TOKEN_PATH); // single use
    return true;
  } catch {
    return false;
  }
}

function tokenIsLive() {
  try {
    return Date.now() - statSync(TOKEN_PATH).mtimeMs <= TOKEN_TTL_MS;
  } catch {
    return false;
  }
}

// -------------------------------------------------------------- sessions ----

const hash = (value) => createHash('sha256').update(value).digest('hex');

function mintSession() {
  const token = randomBytes(32).toString('base64url');
  state.sessions[hash(token)] = { createdAt: Date.now(), expiresAt: Date.now() + SESSION_TTL_MS };
  saveState(state);
  return token;
}

function sessionValid(cookieHeader) {
  const match = /(?:^|;\s*)llm_gate_session=([^;]+)/.exec(cookieHeader ?? '');
  if (!match) return false;
  const session = state.sessions[hash(match[1])];
  return Boolean(session && session.expiresAt > Date.now());
}

// ------------------------------------------------------------------ http ----

function send(res, status, body, headers = {}) {
  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  res.writeHead(status, {
    'content-type': typeof body === 'string' ? 'text/html; charset=utf-8' : 'application/json',
    'cache-control': 'no-store',
    ...headers,
  });
  res.end(payload);
}

async function readJson(req) {
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 100_000) throw new Error('body too large');
  }
  return JSON.parse(raw || '{}');
}

// The tiny WebAuthn client, inlined - no external scripts on an auth page.
const HELPERS = `
const b64uToBuf = (s) => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)).buffer;
const bufToB64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error ?? ('HTTP ' + res.status));
  return res.json();
}
function fail(err) { document.getElementById('msg').textContent = err.message || String(err); }
`;

const PAGE_STYLE = `<style>
body{margin:0;display:grid;place-items:center;min-height:100vh;background:#10142b;color:#dfe3f2;font:15px/1.5 system-ui,sans-serif}
main{text-align:center;padding:2rem;max-width:26rem}
h1{font-size:1.1rem;letter-spacing:.02em}
button{margin-top:1rem;padding:.7rem 1.6rem;border-radius:.6rem;border:0;background:#8b93f8;color:#10142b;font-weight:600;font-size:1rem;cursor:pointer}
#msg{margin-top:1rem;color:#f8a5a5;font-size:.85rem;min-height:1.2em}
p{color:#8d93ad;font-size:.85rem}
</style>`;

const LOGIN_PAGE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Router</title>${PAGE_STYLE}<main>
<h1>LLM router admin</h1>
<p>This panel is protected by a passkey bound to this domain.</p>
<button id="go">Sign in with passkey</button><div id="msg"></div></main>
<script>${HELPERS}
document.getElementById('go').onclick = async () => {
  try {
    const options = await post('/auth/login/options');
    options.challenge = b64uToBuf(options.challenge);
    (options.allowCredentials ?? []).forEach(c => c.id = b64uToBuf(c.id));
    const cred = await navigator.credentials.get({ publicKey: options });
    await post('/auth/login/verify', {
      id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: {
        authenticatorData: bufToB64u(cred.response.authenticatorData),
        clientDataJSON: bufToB64u(cred.response.clientDataJSON),
        signature: bufToB64u(cred.response.signature),
        userHandle: cred.response.userHandle ? bufToB64u(cred.response.userHandle) : null,
      },
    });
    location.href = '/ui/';
  } catch (err) { fail(err); }
};
</script>`;

const ENROL_PAGE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Router enrolment</title>${PAGE_STYLE}<main>
<h1>Enrol this device</h1>
<p>Single-use link. Registers a passkey for ${RP_ID} on this device.</p>
<button id="go">Create passkey</button><div id="msg"></div></main>
<script>${HELPERS}
document.getElementById('go').onclick = async () => {
  try {
    const token = new URLSearchParams(location.search).get('token');
    const options = await post('/auth/enrol/options', { token });
    options.challenge = b64uToBuf(options.challenge);
    options.user.id = b64uToBuf(options.user.id);
    (options.excludeCredentials ?? []).forEach(c => c.id = b64uToBuf(c.id));
    const cred = await navigator.credentials.create({ publicKey: options });
    await post('/auth/enrol/verify', {
      token,
      credential: {
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        clientExtensionResults: cred.getClientExtensionResults(),
        response: {
          attestationObject: bufToB64u(cred.response.attestationObject),
          clientDataJSON: bufToB64u(cred.response.clientDataJSON),
          transports: cred.response.getTransports ? cred.response.getTransports() : [],
        },
      },
    });
    document.getElementById('msg').style.color = '#9be8a9';
    document.getElementById('msg').textContent = 'Enrolled. Redirecting…';
    setTimeout(() => location.href = '/ui/', 800);
  } catch (err) { fail(err); }
};
</script>`;

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}`);
  try {
    // nginx auth_request target. Bearer requests are LiteLLM's to judge.
    if (url.pathname === '/auth/check') {
      const original = req.headers['x-original-authorization'] ?? '';
      if (/^Bearer\s/i.test(original)) return send(res, 204, '');
      if (sessionValid(req.headers.cookie)) return send(res, 204, '');
      return send(res, 401, { error: 'no session' });
    }

    if (url.pathname === '/auth/' || url.pathname === '/auth') {
      return send(res, 200, LOGIN_PAGE);
    }

    if (url.pathname === '/auth/enrol') {
      // The page itself needs no token; the ceremonies do.
      return send(res, 200, ENROL_PAGE);
    }

    if (url.pathname === '/auth/login/options' && req.method === 'POST') {
      if (state.credentials.length === 0) return send(res, 409, { error: 'no passkey enrolled yet' });
      const options = await generateAuthenticationOptions({ rpID: RP_ID, userVerification: 'preferred' });
      putChallenge('login', options.challenge);
      return send(res, 200, options);
    }

    if (url.pathname === '/auth/login/verify' && req.method === 'POST') {
      const body = await readJson(req);
      const expectedChallenge = takeChallenge('login');
      if (!expectedChallenge) return send(res, 400, { error: 'challenge expired - try again' });
      const credential = state.credentials.find((c) => c.id === body.id);
      if (!credential) return send(res, 401, { error: 'unknown passkey' });
      const verification = await verifyAuthenticationResponse({
        response: body,
        expectedChallenge,
        expectedOrigin: ORIGIN,
        expectedRPID: RP_ID,
        credential: {
          id: credential.id,
          publicKey: Buffer.from(credential.publicKey, 'base64url'),
          counter: credential.counter,
          transports: credential.transports,
        },
      });
      if (!verification.verified) return send(res, 401, { error: 'verification failed' });
      credential.counter = verification.authenticationInfo.newCounter;
      const token = mintSession();
      return send(res, 200, { ok: true }, {
        'set-cookie': `${COOKIE}=${token}; Max-Age=${SESSION_TTL_MS / 1000}; Path=/; Secure; HttpOnly; SameSite=Lax`,
      });
    }

    if (url.pathname === '/auth/enrol/options' && req.method === 'POST') {
      const body = await readJson(req);
      // Checked but NOT consumed here: the token dies when enrolment
      // completes, not when the page first asks for options.
      try {
        const age = Date.now() - statSync(TOKEN_PATH).mtimeMs;
        const stored = readFileSync(TOKEN_PATH, 'utf8').trim();
        if (age > TOKEN_TTL_MS || stored !== (body.token ?? '')) {
          return send(res, 403, { error: 'enrolment link is invalid or expired' });
        }
      } catch {
        return send(res, 403, { error: 'enrolment link is invalid or expired' });
      }
      const options = await generateRegistrationOptions({
        rpName: 'LLM Router',
        rpID: RP_ID,
        userName: 'operator',
        attestationType: 'none',
        authenticatorSelection: { residentKey: 'required', userVerification: 'preferred' },
        excludeCredentials: state.credentials.map((c) => ({ id: c.id, transports: c.transports })),
      });
      putChallenge('enrol', options.challenge);
      return send(res, 200, options);
    }

    if (url.pathname === '/auth/enrol/verify' && req.method === 'POST') {
      const body = await readJson(req);
      const expectedChallenge = takeChallenge('enrol');
      if (!expectedChallenge) return send(res, 400, { error: 'challenge expired - try again' });
      if (!consumeToken(body.token)) return send(res, 403, { error: 'enrolment link is invalid or expired' });
      const verification = await verifyRegistrationResponse({
        response: body.credential,
        expectedChallenge,
        expectedOrigin: ORIGIN,
        expectedRPID: RP_ID,
      });
      if (!verification.verified || !verification.registrationInfo) {
        return send(res, 400, { error: 'registration failed' });
      }
      const info = verification.registrationInfo.credential;
      state.credentials.push({
        id: info.id,
        publicKey: Buffer.from(info.publicKey).toString('base64url'),
        counter: info.counter,
        transports: info.transports ?? [],
        enrolledAt: new Date().toISOString(),
      });
      saveState(state);
      const token = mintSession();
      return send(res, 200, { ok: true }, {
        'set-cookie': `${COOKIE}=${token}; Max-Age=${SESSION_TTL_MS / 1000}; Path=/; Secure; HttpOnly; SameSite=Lax`,
      });
    }

    if (url.pathname === '/auth/logout' && req.method === 'POST') {
      const match = /(?:^|;\s*)llm_gate_session=([^;]+)/.exec(req.headers.cookie ?? '');
      if (match) {
        delete state.sessions[hash(match[1])];
        saveState(state);
      }
      return send(res, 200, { ok: true }, { 'set-cookie': `${COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax` });
    }

    return send(res, 404, { error: 'not found' });
  } catch (err) {
    console.error('gate error:', err instanceof Error ? err.message : err);
    return send(res, 500, { error: 'internal error' });
  }
});

if (process.argv[2] === 'issue-token') {
  issueToken();
  console.log(`enrolment token written to ${TOKEN_PATH} (30-minute TTL, single use)`);
  console.log(`enrol at: ${ORIGIN}/auth/enrol?token=<contents of that file>`);
  process.exit(0);
}

server.listen(PORT, HOST, () => {
  console.error(`llm-auth-gate on ${HOST}:${PORT}, rp ${RP_ID}, ${state.credentials.length} passkey(s)`);
  if (state.credentials.length === 0 && !tokenIsLive()) {
    issueToken();
    console.error(`no passkey enrolled - bootstrap token written to ${TOKEN_PATH}`);
  }
});
