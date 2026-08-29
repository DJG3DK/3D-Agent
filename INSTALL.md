# Installing 3D-Agent

3D-Agent is self-hosted: it runs on a machine you control, works on repos on
that machine, and talks to exactly one paid service (OpenRouter). This guide
takes about 15 minutes, most of it waiting for dependencies.

> **Clone it, don't fork it.** Forking is for sending changes back upstream. To
> *run* 3D-Agent, clone it — every file that is specific to your deployment
> (`.env`, `projects.json`, `skills/local/`, the service overrides) is
> gitignored, so `git pull` brings you updates without ever touching your
> configuration.

---

## 1. What you need

| Requirement | Why |
|---|---|
| **Linux host** you control | The agent runs shell commands and manages git worktrees on this box |
| **Python 3.12+** | The agent itself |
| **Node 20+** | The dashboard build and the two review services |
| **Docker** | Every command the agent runs happens inside a container. Without it, the first tool call of the first task fails |
| **PostgreSQL 14+** | Conversation checkpoints, memory, users |
| **An OpenRouter API key** | The only paid dependency — [openrouter.ai/keys](https://openrouter.ai/keys) |
| **pm2** *(optional)* | To run it as a managed service rather than in a terminal |

A small VPS is enough. The agent is not compute-heavy; the models run
elsewhere.

---

## 2. Install

```bash
git clone https://github.com/DJG3DK/3D-Agent.git
cd 3D-Agent
./install.sh
```

The installer asks three questions — your Postgres DSN, your OpenRouter key,
and an admin email — and derives everything else. It will:

- check every prerequisite and stop with a specific message if one is missing
- generate `AUTH_SECRET_KEY` and a router master key **in the correct format**
  (see the footgun in §6)
- write `.env` and `services/llm-router/.env` with `600` permissions
- create the database if it doesn't exist
- build both Python environments
- build the sandbox container image (includes headless Chromium, so the agent
  can look at frontends it builds)
- build the dashboard

It is safe to re-run: every step detects what already exists, and it never
overwrites an existing `.env` or `projects.json`.

```bash
./install.sh --dry-run     # show every action, change nothing
./install.sh --yes         # unattended; reads PG_DSN / OPENROUTER_API_KEY from the environment
```

---

## 3. Start it

```bash
# the model router first — everything resolves model aliases through it
services/llm-router/venv/bin/litellm --config services/llm-router/config.yaml --port 4000

# then the agent (serves the dashboard itself; there is no separate frontend process)
.venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8100
```

Or under pm2:

```bash
pm2 start ecosystem.config.js
pm2 start services/llm-router/ecosystem.config.js
pm2 save
```

Open **http://127.0.0.1:8100**.

> **The first admin password is printed once, to the server log, on first
> startup.** Capture it. If you miss it, delete the row from the `agent_users`
> table and restart to re-seed.

Admin accounts are required to set up TOTP 2FA on first login.

---

## 4. Add a project

The agent needs at least one project to work in. Either:

- **Dashboard** — Settings → Projects → enter an absolute path → review what
  was detected → Create.
- **CLI** — `.venv/bin/python scripts/add_project.py /path/to/your/repo`
  (add `--yes` for an unattended install).

Both inspect the directory, propose a configuration, and let you approve it.
Read §5 before clicking through it.

Onboarding creates a **git worktree** of your repo under
`AGENT_SANDBOX_ROOT`. The agent works there on a per-task branch and never
commits to your working checkout.

### Push access (deploy keys)

The agent itself never pushes — `git push` is on its blocked-command list.
After you approve a merge, the review service pushes from your project's live
checkout, and that push is **best-effort**: if it cannot authenticate, the
merge and the deploy still succeed and your remote quietly stays behind.

So each project gets its own **deploy key** — an SSH key scoped to one
repository rather than your whole account. Expand a project under
Settings → Projects and use **Push access**:

1. **Generate key** — the server mints an ed25519 keypair. You never handle
   the private half.
2. Copy the public key it shows you.
3. On GitHub: repo → **Settings** → **Deploy keys** → **Add deploy key**,
   paste it, and tick **Allow write access**. Without that box the key can
   read but every push is rejected.
4. **Test connection** — this runs `git ls-remote` exactly as the push will,
   so a green result means the push will authenticate.

You can paste an existing private key instead. It must have **no passphrase**,
since nothing can type one during an unattended push; a passphrase-protected
key is rejected at paste time rather than failing at merge time.

Keys are stored under `keys/` with `0600` permissions (gitignored), and each
one is wired to a single repo via that repo's own `core.sshCommand` — so one
project's key never signs another's git operations. No endpoint ever returns a
private key.

If your `origin` is an **HTTPS** URL, an SSH deploy key cannot authenticate
it; switch the remote to SSH or configure a credential helper on the host. The
panel tells you which case you're in.

---

## 5. Read this before onboarding a project

The review step is a safety gate, not a formality.

**Detection proposes; you decide.** Anything the installer cannot verify
arrives switched **off**, with the reason attached. The important case is test
scripts that make network calls. A test suite that talks to a live service can
*act* on production — the deployment this was built on had a `test:routes`
script that POSTed real trade orders at a running bot. No static analysis can
tell "hits a test server" apart from "hits your production system", so those
scripts are flagged and disabled, and you enable them only after reading them.

If your repo defines a `test:review` script naming the suites that are safe in
a detached checkout, that is trusted over the aggregate `test` script.

**Containment.** Projects may only be onboarded from inside
`AGENT_PROJECT_ROOTS` (set in `.env`, defaults to your home directory), judged
after symlink resolution. Keep it as narrow as your layout allows: onboarding
grants an agent write access to what it points at.

---

## 6. Configuration reference

Everything lives in `.env` (see `.env.example` for the full list).

| Variable | Notes |
|---|---|
| `LANGGRAPH_PG_DSN` | Postgres DSN for checkpoints, memory and users |
| `LITELLM_BASE_URL` / `LITELLM_API_KEY` | The router. `LITELLM_API_KEY` **must equal** `LITELLM_MASTER_KEY` in `services/llm-router/.env` |
| `AUTH_SECRET_KEY` | Encrypts TOTP secrets at rest. **Must decode to 16, 24 or 32 raw bytes.** `openssl rand -hex 32` produces 48 bytes and will *not* work — use `python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`. Rotating it locks out every 2FA user permanently |
| `AGENT_PROJECT_ROOTS` | Colon-separated roots projects may be onboarded from |
| `AGENT_SANDBOX_ROOT` | Where per-project worktrees are created |
| `DEFAULT_BUDGET_USD` | Per-task spend ceiling |
| `SMTP_*` | Optional; only used for password-reset email |

**Files that are yours, not the project's** (all gitignored — they survive
`git pull`):

| Path | What it holds |
|---|---|
| `.env`, `services/llm-router/.env` | Secrets |
| `projects.json` | Your projects (written by the wizard) |
| `skills/local/` | Your own domain knowledge — see `skills/local/README.md` |
| `services/*/builtin-projects.local.js` | Optional review/deploy overrides — see the `.example` files |
| `memory/*.md` | Per-project memory the agent maintains |

---

## 7. Optional: the review gate

The agent can ship code on its own. The review gate makes it prove itself
first: an independent model reviews every commit in an isolated worktree, runs
the project's real checks, and nothing merges until it passes.

```bash
node services/commit-reviewer/reviewer.js      # zero npm dependencies
cd services/agent-review && npm install && node server.js
```

Projects onboarded through the wizard are picked up automatically. See
`services/commit-reviewer/README.md`.

---

## 8. Choosing models

Everything routes through named aliases (`agent-coder`, `agent-planner`,
`agent-reviewer`, …) defined in `services/llm-router/config.yaml`. The shipped
pins are a reasonable starting point.

Change them from **Settings → Models** in the dashboard, which shows each
model's price, agentic-arena standing and knowledge cutoff, and each provider's
latency, uptime and caching support. Model changes take effect at the next
router restart.

---

## 9. Troubleshooting

**The first tool call of the first task fails.** The sandbox image isn't
built: `docker build -t 3d-agent-sandbox:latest docker/agent-sandbox/`

**Every model call 401s.** `LITELLM_API_KEY` in `.env` doesn't match
`LITELLM_MASTER_KEY` in `services/llm-router/.env`.

**The app won't start, complaining about the auth key.** `AUTH_SECRET_KEY`
doesn't decode to 16/24/32 raw bytes — see §6.

**The dashboard is blank.** The bundle wasn't built:
`cd frontend && npm ci && npm run build`, then restart the backend.

**A project won't onboard: "outside the configured project roots."** Its path
isn't under `AGENT_PROJECT_ROOTS`. Widen it deliberately, or move the repo.

**Checks never run for a project.** Detection found no `typecheck`/`lint`/
`test` scripts, or the only test script was flagged as network-touching and
left disabled. Check Settings → Projects.

---

## 10. Updating

```bash
git pull
./install.sh          # re-runs safely; installs any new dependencies
```

Restart the agent afterwards. Your `.env`, projects, skills and memory are
untouched.

---

## License

PolyForm Noncommercial 1.0.0 — source-available, not open source. Use, modify
and share it freely for any noncommercial purpose. Commercial use requires a
separate license; open an issue to start that conversation.
