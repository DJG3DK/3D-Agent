# Changelog

## v0.2.0 — landing page, a frontend test suite, Node 24 (pre-release)

**2026-08-30**

Thirteen commits on top of v0.1.0. The headline is that the login screen
is no longer the front door, and the frontend is no longer untested.

### A public landing page

Anyone arriving from a shared link used to hit a password box for a
console they cannot enter. There is now a real page in front of it —
the pipeline, the review gate, the console, the controls the model cannot
override — built from the README's own copy, so the two cannot drift into
telling different stories. Sign-in moved to the top right; the link to the
source moved the other way, off the login card and onto the page where
someone who *cannot* sign in actually has somewhere to go.

The eight screenshots are the ones already in `docs/`, re-encoded to WebP:
2.4MB of PNG becomes 268KB, content-hashed by Vite so they inherit the
immutable cache year.

The page is indexable now (`noindex` dated from when this URL was nothing
but a password box), with a `meta description` and a `canonical` — the SPA
fallback answers 200 with the same shell for every path, so without one a
crawler can index an unbounded set of URLs that are all the same page.

### The frontend has tests

There was no test framework at all: every behaviour was guarded by nothing
but a typecheck. Adds vitest + Testing Library + jsdom, wired into CI
*ahead* of the build. **141 tests.** Branch coverage is 73% against 45% of
statements, and that gap is deliberate — the decision logic is covered;
the rest is chart and form markup where a test asserts little beyond
"React rendered".

Coverage is reported, not enforced. A threshold that fails CI on an
unrelated refactor teaches people to delete tests.

### Node 24

Node 20 left maintenance in April 2026, and its EOL had started to cost
something concrete: the test toolchain had to be pinned back a major
version each to keep supporting it. CI, the installer's prerequisite check
and the documented requirement all move to **24** together — CI testing
one version while the docs tell people to install another verifies
nothing.

**This is the one upgrade note.** If you are on Node 20, `install.sh` will
now refuse until you upgrade. Nothing else in this release requires action.

Re-verified end to end on the new floor: Debian 13 (Python 3.13.5, Node
24.20.0) against a real postgres:16 — `install.sh` completes, then 694
Python tests and 141 frontend tests pass in that same container.

### Fixes

- **Sidebar categories could not be collapsed while a task was running.**
  Expansion was `manuallyExpanded.has(cat) || searching || selectedCategory
  === cat`, a shape that can only ever *add* expansion — so clicking the
  header did nothing, with no indication why. Underneath it,
  `selectedCategory` resolved the category of a *running* task, which is
  filtered out of the category lists and shown in the Running group
  instead: selecting one opened a category it is not a member of.
- **A signed-out visitor was sent past the landing page to the login
  form**, because the opening `getMe()` 401 fired the session-expiry
  handler. An expired session still goes straight to the form.
- **The landing page's nav was declared sticky and never stuck.** The page
  carried `overflow-x: hidden` to stop horizontal scroll; setting one axis
  to `hidden` computes the other to `auto`, which makes the element a
  scroll container — and a scroll-container ancestor silently disables
  `position: sticky` on everything inside it. `overflow-x: clip` clips the
  same overflow without establishing one.
- **BalanceStrip crashed on a 200 with an unexpected body.** `!balance`
  passed for `{}`, then `.toFixed` threw — and it renders inside the
  Sidebar, so the ErrorBoundary blanked the whole console over a
  decorative credit strip.
- **`HEAD` returned 405 on every file at the dist root.** FastAPI's
  `@app.get` registers exactly the methods named, unlike Starlette's plain
  `Route`, which folds `HEAD` in — so a crawler sizing an `og:image`
  before fetching it got a 405.
- **Those files were served with `max-age=86400` and no way to
  revalidate.** They are `no-cache` now, with conditional requests
  answered properly: `FileResponse` sets etag and last-modified but never
  checks them, and only populates them when handed a `stat_result`, so
  both had to be wired up for a revalidation to cost a 304 rather than the
  whole file.
- A malformed WebSocket frame threw a bare `SyntaxError` out of
  `onmessage`. The stream survived either way, but the log said nothing
  about which socket produced it.
- The agent dashboard had `og:title` and `og:description` but no
  `og:image`, so its link preview was a `summary_large_image` with nothing
  to show.

### Requirements

Linux, Python 3.12+, **Node 24+**, Docker, PostgreSQL 14+, and an
OpenRouter API key. pm2 optional.

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

### Hardening before release

A full external review of the codebase ran before this tag; everything it
found at critical or high severity is fixed here, with a regression test each.
Two were serious enough to be worth naming:

- **Host code execution via git hooks.** Every git command runs on the host
  with its working directory inside the agent-writable worktree, and a
  worktree's `.git` is a pointer *file* living in that same writable tree.
  Rewriting it re-aimed git at an agent-controlled directory, hooks included,
  so the post-build commit executed agent-authored code as the server user.
  Hooks are now disabled on every invocation and the pointer is verified
  against server-owned config.
- **Sandbox mount escape.** The container's bind mounts were computed from
  state read out of the worktree — a `node_modules` symlink and the `.git`
  pointer — so the agent could choose what the host mounted into its own
  container (`ln -s /home node_modules` produced `-v /home:/home:ro`). Mount
  targets are now validated against the project's own paths.

Also fixed: alerts that ignored per-user repo scoping, a fail-open in user
creation, streams that dropped data on an unclean reconnect, three
"documented but broken" issues that would have hit the first fresh install,
and the absence of CI.

### Known limits

- **Pre-1.0, and field-tested on exactly one machine.** Expect rough edges on a
  different distro, Postgres version, or non-root install.
- **Linux host required.** The sandbox is Docker; there is no Windows path.
- **Remote access needs HTTPS or an SSH tunnel.** The session cookie is
  `Secure`, so plain HTTP works only on `localhost`/`127.0.0.1`. A LAN or VPN
  address over plain HTTP will drop the cookie and bounce you back to the login
  page. `install.sh` can set up nginx + Let's Encrypt for a domain.
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
