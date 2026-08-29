# Security

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue.

Use GitHub's private reporting: **Security → Report a vulnerability** on
[github.com/DJG3DK/3D-Agent](https://github.com/DJG3DK/3D-Agent/security/advisories/new).
That opens a channel only the maintainer can see.

Include what you'd want to receive: what you did, what happened, what you
expected, and the smallest reproduction you have. If it involves the agent
executing something it shouldn't, the exact prompt or task text matters —
that *is* the payload.

This is a single-maintainer project, so expect a first response in days, not
hours. You'll get an acknowledgement, a fix or an explanation of why it isn't
one, and credit in the release notes unless you'd rather not be named.

## What this project is, threat-model-wise

3D-Agent runs an LLM that writes and executes code against your repositories.
That is the product, not a bug — so it's worth being precise about which
capabilities are intended and which would be real vulnerabilities.

**Working as designed** (please don't report these as vulnerabilities):

- The agent runs arbitrary shell commands. It does so inside a Docker
  container with only that project's worktree mounted, `--cap-drop ALL` and
  `--security-opt no-new-privileges` — but it is genuinely running model-chosen
  commands, on purpose.
- The agent can modify any file in a project you onboarded. That's the job.
- An admin can point onboarding at any directory inside `AGENT_PROJECT_ROOTS`.
  Narrowing that root is the operator's control.
- Model output is not trusted-but-verified so much as *gated*: an independent
  review service and (optionally) a human approve every merge.

**Genuine vulnerabilities** — please do report:

- Escaping the sandbox container, or reaching host paths outside the mounted
  worktree from an agent tool call.
- Reading or exfiltrating a secret the agent shouldn't see: `.env` contents,
  another project's credentials, a deploy key, `AUTH_SECRET_KEY`, session
  tokens.
- Authentication or authorization bypass: acting as another user, reaching an
  admin endpoint without an admin session, accessing a repo outside your
  `allowed_repos`, defeating TOTP.
- Onboarding a path outside `AGENT_PROJECT_ROOTS`, or getting a command into
  a project's check/build config that the server didn't itself propose.
- Prompt injection that escalates *privilege* — content in a repo, a web page
  or a task description that causes the agent to take an action the operator's
  settings should have prevented (bypassing the approval gate, pushing without
  review, disabling a control).
- Anything that lets an unauthenticated request reach a code-execution path.

## Deploying this safely

- **Never expose the dashboard directly to the internet.** It is an operator
  console. Put it behind a VPN, an SSH tunnel, or a reverse proxy with its own
  authentication. The app has its own login and TOTP, but it is not designed
  to be an internet-facing surface.
- **Keep `AGENT_PROJECT_ROOTS` narrow.** Onboarding grants an agent write
  access to whatever it points at.
- **Treat the review gate as load-bearing.** `Final merge review` and the
  independent reviewer exist because a model can be confidently wrong; turning
  both off means unattended merges to your live repos.
- **Deploy keys, not account keys.** Each project gets an SSH key scoped to one
  repository (Settings → Projects → Push access) rather than a credential that
  can reach everything you own.
- **Budget ceilings are a safety control too.** `DEFAULT_BUDGET_USD` bounds a
  runaway loop's cost.

## Supported versions

Pre-1.0. Fixes land on `main`; there are no backported release branches yet.
Run the latest commit.
