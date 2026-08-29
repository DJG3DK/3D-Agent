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
3. **Support another stack.** Check detection (`agent/provisioning.py`) is
   npm/pnpm/yarn plus a basic Python path. Go, Rust, Ruby and Elixir projects
   currently onboard with no checks detected.
4. **Improve the docs.** If [INSTALL.md](INSTALL.md) misled you, that's a bug
   worth a PR.

## Setting up to develop

```bash
git clone https://github.com/DJG3DK/3D-Agent.git
cd 3D-Agent
./install.sh                       # see INSTALL.md
.venv/bin/python -m pytest -q      # ~600 tests, seconds, no network needed
```

Frontend:

```bash
cd frontend
npm ci
npm run dev        # dev server
npm run build      # production bundle -> frontend/dist
npx tsc --noEmit -p tsconfig.app.json
npm run lint
```

The node services have their own checks: `node --check` on each file, and
`node tests/test_projects_config_merge.js`.

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
worktree's `.git` is a file rather than a directory.

## Pull requests

- One concern per PR. A refactor bundled with a fix is hard to review and hard
  to revert.
- Say what breaks if you're wrong. Reviewers calibrate on that.
- Run the suites above before opening. CI is not going to catch it for you yet.
- Small PRs get read the same week. Large ones may sit — open an issue first
  if you're planning something big, so you don't build the wrong thing.

## Security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md) for private
reporting.
