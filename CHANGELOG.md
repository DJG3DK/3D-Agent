# Changelog

## v0.1.0 — first public release (pre-release)

**2026-08-29**

The first public cut of 3D-Agent. It has run continuously on one deployment for
several months against three real repositories — a live trading bot, an
e-commerce monorepo and a Next.js site — but this is the first time anyone else
can install it. Treat it accordingly: see *Known limits* below.

### What it does

Give it a plain-English goal against a repository you've onboarded. It plans the
work, writes the code, runs that project's **real** test suite, and ships it —
with an independent review gate that must pass before anything merges.

- **Plan → build → verify → review → ship.** Every task runs in a git worktree
  of your live repo, on its own branch, inside a Docker container that can see
  only that worktree.
- **A second model reviews every diff** in an isolated checkout before a merge
  is possible, and optionally a human approves after that. Nothing merges on
  the agent's say-so.
- **Planning Chat** — a separate conversational mode for research and design
  that remembers what it learns and can hand a finished plan to the build
  pipeline.
- **Memory that compounds.** Completed tasks are consolidated into per-project
  memory; a cartographer keeps a structural map of each codebase current.
- **Budget ceilings per task**, so a runaway loop costs a known maximum.
- **Model routing by role.** Planner, coder, reviewer, summarizer and the rest
  are named aliases you repin from the dashboard, with live pricing, agentic
  benchmark standing and per-provider latency/uptime shown at the point of
  choice.

### Setting it up

- **`./install.sh`** takes a fresh clone to a running agent. It checks
  prerequisites, generates secrets in the formats the app actually requires,
  creates the database, builds the sandbox image and the dashboard, and is safe
  to re-run — which is also the upgrade path. `--dry-run` and `--yes` included.
- **Project onboarding from the dashboard** (Settings → Projects) or
  `scripts/add_project.py`. It inspects a directory, proposes a configuration,
  and asks you to confirm it.
- **[INSTALL.md](INSTALL.md)** is the full guide: requirements, configuration
  reference, troubleshooting for the real failure modes, and the update path.

### The safety posture, stated plainly

This runs a model that writes and executes code against your repositories. The
controls are the product, not an afterthought:

- Onboarding **proposes, you approve**. Anything that can't be verified arrives
  switched off with the reason attached. A test script that makes network calls
  is disabled by default — a suite that talks to a live service can *act* on
  production, and no static analysis distinguishes "hits a test server" from
  "hits your production system".
- Projects can only be onboarded from inside `AGENT_PROJECT_ROOTS`, judged after
  symlink resolution. The worktree location and project name are derived by the
  server, never accepted from a request.
- Check and build commands are matched against what the server itself proposed,
  so a client cannot introduce a command for the review or deploy service to
  run.
- Each project pushes with its **own deploy key**, scoped to one repository,
  wired to that repo's `core.sshCommand` alone.
- The agent cannot `git push` — that's on its blocked-command list.

See [SECURITY.md](SECURITY.md) for the full threat model, including which
capabilities are intended and which would be genuine vulnerabilities.

### Known limits

- **Pre-1.0, and field-tested on exactly one machine.** Expect rough edges on a
  different distro, Postgres version, or non-root install.
- **Linux host required.** The sandbox is Docker; there is no Windows path.
- **Stack detection covers npm/pnpm/yarn and basic Python.** Go, Rust and Ruby
  projects onboard fine but arrive with no checks detected — you add commands by
  hand.
- **The sandbox image ships Node and Python only.** A project needing another
  toolchain needs the image extended.
- **No migrations story yet.** Upgrades are `git pull` plus `./install.sh`; the
  schema is created on first start and has not needed a migration path so far.
- **Costs real money.** Every task calls a paid model. Set
  `DEFAULT_BUDGET_USD` deliberately.

### Requirements

Linux, Python 3.12+, Node 20+, Docker, PostgreSQL 14+, and an OpenRouter API key
(the only paid dependency). pm2 optional.

### License

PolyForm Noncommercial 1.0.0 — source-available, not open source. Free for any
noncommercial use; commercial use needs a separate licence.
