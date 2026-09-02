# Changelog

## v0.3.0 — runtime limits in the console, one save bar everywhere, planning that stops for the right reasons (pre-release)

**2026-09-02**

Fourteen commits on top of v0.2.0. The headline is that the dials which
used to need a file edit and a restart are now in the console, and that a
planning turn is no longer killed for the crime of taking a while.

### Runtime limits, from the console

Fourteen limits were module constants and environment variables, so
retuning one meant editing a file and restarting — and a restart is
exactly what you cannot do while the thing you want to retune is running.
They live in the store now (**Settings → Runtime limits**, admin only):

    planning turn budget      planning stall timeout    default task budget
    model calls per run       tool calls per run        review wait timeout
    model call timeout        planning model timeout    lint timeout
    typecheck timeout         test suite timeout        review test timeout
    frontend build timeout    default shell timeout

Every one is read at the point of *use*, so a change lands on the next
turn or task and never mutates something already in flight. Values are
clamped to each knob's bounds rather than rejected; unknown names are
rejected, because a typo must not sit in the database looking like
configuration. Existing environment variables still seed the defaults.
Seconds render beside their plain-units equivalent (1200 → "20 min"), and
the label carries the full reasoning as a tooltip.

Worth knowing: `npm test` was capped at 180s, and a repo that chains
dozens of suites can exceed that. An abort reads to the agent as a
*failing* test rather than a slow one, so it would try to fix a suite that
was merely long. That cap is now a dial.

Deliberately not exposed: auto-approve and merge review. Those change what
the agent is *permitted* to do; these are dials on effort.

### One save bar, on both pages that have something to save

Every editable card on the settings page carried its own Save button — a
row of controls disabled almost all of the time, and an edit in a card you
had scrolled past was easy to abandon. There is now one bar, pinned
bottom-right, that appears only when something is genuinely dirty, names
the count, and offers Discard. On a phone it goes full width above the tab
bar rather than floating over it.

The **model configuration page** gets the same flow. Its static
"Save N changes" button sat at the very bottom of three groups of rows, so
a pin changed in the top group was just as easy to lose. Same bar, same
Discard, and a failed save is reported beside the button that caused it
with the edit kept.

The settings page itself was also re-laid: three cards per row where the
grid arithmetic had silently only ever allowed two, Telegram and Projects
paired into one row, spinner buttons gone from numeric fields where they
sat on top of the digits.

### Planning turns that end for the right reasons

- **Bounded by silence, not by the clock.** A planning turn was killed at
  30 minutes while actively streaming — $2.50 spent, no plan saved. A
  duration ceiling selects against exactly the work it should protect.
  The watchdog now fires only when a turn produces *nothing* for 20
  minutes (adjustable above); duration is unbounded on purpose, and the
  budget ceiling remains the only thing that stops work abruptly.
- **Why it stopped is recorded.** The reason used to be a live WebSocket
  event and nothing else — refresh and it was gone. Outcomes are now
  classified (`completed / stopped / stalled / budget / error`), stored
  with the session, and shown when you open one that ended badly, with the
  note that the context is still there and the turn can be continued.
- **The re-read loop.** One session read `src/core/bot.js` 129 times,
  spent its whole $8 ceiling and produced nothing: planning shared the
  build task's summarization window, which was too small to hold the four
  files the question spanned, so it dropped what it had just read and read
  it again. Planning has its own window now, plus a per-turn per-file read
  ledger that warns at 6 reads and refuses at 14.
- **Live cost, and a UI that notices the turn ended.** Cost read $0 for
  the whole turn because it was banked only at the end; it is mirrored as
  it accrues now. And a half-open socket left the browser showing thinking
  bubbles forever — a liveness watchdog treats 70s of silence as death and
  reconnects, with `running` read back from the server.

### Fixes

- **The investigator subagent was never used.** 281 coder calls, 207
  test-writer, zero investigator, across every task since the router
  rework. Its delegation was written as a preference ("prefer the
  investigator for multi-file research") and the coordinator, holding
  `rg` itself, always declined. It is a rule now, with a trigger it can
  evaluate. Prompt-only — worth re-checking the per-role counts.
- **The public demo went down on two 429s a minute apart.** Its model was
  pinned to a single provider with fallbacks off, the one alias in the
  config with no router-level fallback either. Three layers now:
  preferred provider, any other provider of the same model, then a cheap
  proven tool-caller if the model is unavailable everywhere.
- **The router exited at startup instead of degrading** when the database
  was connected, because LiteLLM shells out to the `prisma` CLI to check
  migrations and could not find it outside the venv. The pm2 config puts
  the venv on `PATH`.
- **The mail agent's model spiralled on reasoning.** One "can you see
  starred mail" turn spent 40K reasoning tokens and $0.72 producing
  nothing visible, since reasoning streams as empty chunks. Reasoning
  effort is pinned low for that role: a tool-driven mail agent needs quick
  tool calls, not deep chains.

### Known limits

The half-open-socket gap fixed for planning streams still exists for task
streams; a task's UI can show it running after the socket has died.

### Requirements

Unchanged: Linux, Python 3.12+, Node 24+, Docker, PostgreSQL 14+, and an
OpenRouter API key. pm2 optional. **740 Python tests, 168 frontend
tests** pass on this release.

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
