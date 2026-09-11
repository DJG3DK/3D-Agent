# Contributing

Thanks for looking. A few things worth knowing before you spend time.

## First, the licence

This project is under **PolyForm Noncommercial 1.0.0** — source-available, not
open source. You can read, run, modify and share it for any noncommercial
purpose; commercial use needs a separate licence.

By opening a pull request you agree your contribution is licensed on the same
terms, and that the maintainer may also license the project (including your
contribution) commercially. If you're not comfortable with that, please open an
issue to discuss rather than sending code.

## The most useful things you can do

In rough order of value:

1. **Report what actually broke.** This runs on a lot of moving parts —
   Postgres, Docker, a model router, two node services. Real failure reports
   from a machine that isn't the maintainer's are worth more than features.
2. **Make it run somewhere new.** It's been exercised on one Linux box. Other
   distros, other Postgres versions, non-root installs, ARM — every one of
   those will surface something.
3. **Support another stack.** Check detection (`agent/provisioning.py`) now
   covers npm/pnpm/yarn, Python, Go, Rust, Ruby, and Makefile targets as a
   fallback. Elixir, Java, PHP and .NET still onboard with no checks detected,
   which makes the review gate a no-op for them. The shape to copy is
   `_detect_go_checks`, and `scripts/verify_stack_checks.py` runs whatever you
   add against a real toolchain in a container.
4. **Improve the docs.** If [INSTALL.md](INSTALL.md) misled you, that's a bug
   worth a PR.

## Setting up to develop

**You do not need to install the agent to work on it.** A fresh clone with no
`.env`, no database, no Docker and no API key runs every check CI runs. That
is deliberate: `tests/conftest.py` fills in placeholder environment variables,
so `agent.config` imports without a real configuration.

**Node 24 or newer.** The floor is 24 (Node 20 left maintenance in April
2026); `install.sh` and CI both pin it. On Node 22 the node service checks can
hang rather than fail, which is a miserable way to discover a version mismatch:

```bash
node --version      # v24.x or newer
python3 --version   # 3.12+
```

The exact four things CI does, in order, from a clean clone:

```bash
# 1. Python -- the suite plus lint. No network, no database, no .env needed.
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .

# 2. Frontend -- typecheck, lint, tests, build.
cd frontend && npm ci
npx tsc -b --noEmit
npm run lint
npx vitest run
npm run build
cd ..

# 3. The two Node services -- syntax on every file, then their unit tests.
for f in services/*/*.js services/shared/*.js; do node --check "$f"; done
node tests/test_projects_config_merge.js
node tests/test_reviewer_preexisting.js
node tests/test_preflight.js
node tests/test_service_env.js

# 4. Shell -- the scripts, and an installer dry-run.
bash -n install.sh scripts/*.sh
PG_DSN=postgresql://placeholder@localhost:5432/placeholder \
  OPENROUTER_API_KEY=placeholder ./install.sh --dry-run --yes
```

The dry-run needs `--yes` and those two placeholders: without them it reads
its three answers from `/dev/tty`, which does not exist in a pipeline or in
most editor terminals, and the failure looks like a broken installer rather
than a missing flag. It writes nothing either way.

If all four pass locally, CI will pass. If one behaves differently on your
machine, that is a bug worth reporting on its own — the point of the offline
setup is that a contributor's box and CI agree.

To actually *run* the agent (Postgres, Docker, a router, the two services),
follow [INSTALL.md](INSTALL.md), and check the result with
`.venv/bin/python scripts/doctor.py`. [docs/architecture.md](docs/architecture.md)
is the map of what those processes are and which file holds what.

## House style

The code in this repo is commented unusually heavily, and deliberately so. The
rule is: **a comment explains something the code can't**, and most of them
record an incident. "Read them on every response, not just on errors" is
useful; "loop over the headers" is not.

Concretely:

- Explain *why*, especially when the obvious approach was wrong. If you fixed
  something that had failed in production, say what failed.
- Don't narrate the next line, restate the function name, or annotate the
  change ("added this to fix X") — that's a commit message, not a comment.
- Match the surrounding density. Some modules are dense with hard-won context;
  a one-line helper doesn't need a preamble.

Naming and structure: follow whatever the file already does.

## Tests

**A PR that changes behaviour needs a test that fails without it.** Not
coverage for its own sake — a test that would have caught the bug.

Look at `tests/test_provisioning.py` for the shape this project favours: the
test names state the property being protected, and the docstrings say why the
property matters. The best test in that file encodes a real incident — a
project's test suite POSTed live trade orders at a running production service,
so onboarding now refuses to auto-enable a script that makes network calls.

Prefer real fixtures over mocks where it's cheap: the provisioning tests build
actual git repositories in `tmp_path`, because a mocked git can't catch that a
worktree's `.git` is a file rather than a directory. `tests/test_project_lock.py`
is the other shape worth copying — it drives real SQL through an injected
connection, so the advisory lock's behaviour is pinned without CI needing a
Postgres.

## Pull requests

- One concern per PR. A refactor bundled with a fix is hard to review and hard
  to revert.
- Say what breaks if you're wrong. Reviewers calibrate on that.
- Run the four blocks under *Setting up to develop* before opening. CI runs
  exactly those on every pull request, all offline — no secrets, no database,
  no model calls.
- Small PRs get read the same week. Large ones may sit — open an issue first
  if you're planning something big, so you don't build the wrong thing.

## Security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md) for private
reporting.
