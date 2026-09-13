# Roadmap: packaging, Windows, and projects that are not local

Written 2026-09-13. Every claim about the current code below was checked
against the tree, not remembered.

This is a plan, so unlike `docs/architecture.md` it *is* allowed to talk about
things that do not exist yet. Anything describing the present tense has a file
path next to it.

---

## What we are actually trying to fix

Today the agent must be installed on a Linux box that the projects also live
on. That is two separate constraints wearing one coat:

1. **The host must be Linux** — a packaging problem.
2. **The projects must be on the same machine** — an execution problem.

They are independent, they have different fixes, and conflating them is why
this looks bigger than it is.

A third thing is *not* a constraint, despite the README's framing: **a domain
was never required.** `install.sh` prompts for nginx + certbot and skips it
happily; the agent binds `127.0.0.1:8100`; INSTALL.md §3a already documents an
SSH tunnel as the alternative. "Local edition" is a packaging and onboarding
job, not an architecture change.

---

## What ties us to Linux right now

Four things, and only two of them are interesting.

| Thing | Where | Severity |
|---|---|---|
| Process-group kill | `agent/tools/shell.py` — `start_new_session=True` + `os.killpg`, so killing host-side git takes its children with it | Small. The only POSIX-only construct in the Python tree; Windows wants `CREATE_NEW_PROCESS_GROUP` + `taskkill /T` |
| Same-path bind mounts | `agent/tools/sandbox.py` — mounts host absolute paths into the container **at the same absolute path** (`-v {main_git}:{main_git}:ro`), because a worktree's `.git` is a pointer file naming an absolute path | **This is the design constraint.** `C:\repo\.git` cannot be mounted at `C:\repo\.git` inside a Linux container |
| Installer and process manager | `install.sh` (bash, apt/pacman/dnf), `ecosystem.config.js` (pm2) | Medium, and largely sidestepped by shipping containers |
| Hard dependencies | Postgres, Docker, Node, Python | All cross-platform; the problem is asking a laptop user to install four things |

The agent core, LangGraph, the router, the dashboard and both Node review
services are already portable. Nothing there needs porting.

---

## Where execution actually happens

This matters for the remote-projects work, because it is a much smaller
surface than it looks. Everything funnels through four places:

| Seam | File | What runs through it |
|---|---|---|
| Sandboxed shell | `agent/tools/sandbox.py` → `run_shell_sandboxed` | Every command the model runs, and the whole check suite (`agent/tools/checks.py`) |
| Host shell | `agent/tools/shell.py` → `run_shell` | Host-side git only (`agent/tools/git.py`) |
| Direct file I/O | `agent/tools/files.py` | The `read` / `write` / `edit` tools |
| The reviewer | `services/commit-reviewer` | Its own git and check runs, in Node |

Four. That is the whole list.

---

## Answering the question that prompted this

> If my projects are not local, does the backend have to be installed on the
> VPS?

**No.** `run_shell_sandboxed` builds a complete `docker run …` command line.
Point `DOCKER_HOST` at `ssh://user@host` and that same command executes on the
remote box, against remote paths, with the container's mounts resolving
remotely — no change to how the command is built. That single environment
variable moves *every model command and every check* to the remote machine.

What the remote box needs is **sshd, Docker and git**. Not our backend, not
Postgres, not Python. Tasks, memory, checkpoints, the audit log and the
dashboard all stay on the machine running the control plane.

What does *not* ride along on that trick, and is therefore the real work of
M3: `files.py` (direct local filesystem I/O) and the Node reviewer.

---

## Milestones

Each is independently shippable and independently useful. Ordering is by
dependency, not by appetite.

### M0 — Portability groundwork *(no user-visible change)* — **done, d2c2747**

* An executor interface with exactly one implementation: today's local
  behaviour. Nothing changes; the seam simply exists.
* Fix the process-group kill in `shell.py` so it degrades correctly off Linux.
* Make the sandbox's mount paths come from one place, so "host path equals
  container path" becomes a property we can satisfy deliberately rather than
  an accident of the host layout.

**Done when:** the full suite passes with no behaviour change, and the mount
paths are computed in one function.

### M1 — The container bundle *(this is "the Windows package")* — **core done**

A `docker compose` stack — agent, Postgres, router, both reviewers — that runs
identically wherever Docker Desktop runs: Windows, macOS, Linux.

* The agent image gets the Docker CLI and the host socket, so it spawns
  *sibling* sandbox containers rather than nesting.
* Projects mount at a fixed root (`/projects/<name>`) inside the agent
  container, and the sandbox uses that same path — which is what makes the
  same-path constraint above hold by construction instead of by luck.
* First run generates secrets, creates the admin account and prints the URL.

**Done when:** a Windows machine with Docker Desktop runs `docker compose up`,
reaches the dashboard, and completes a task against a repo cloned **locally on
that machine**.

**Risk to watch:** the socket mount means the sandbox containers are siblings
on the host daemon, so their bind mounts are resolved by the *host*, not by the
agent container. The fixed `/projects` root is what keeps those two views
identical. If this is wrong, we find out here — which is why it is M1 and not
M4.

**Settled, 2026-09-13.** Built and run on Linux: all four health checks green,
and a sibling container spawned by the agent container read a real repo's file
contents *and* its git history through the map. Two things the build found that
a review would not have: `litellm` needs its `[proxy]` extra in a clean image
(the host install had those dependencies already, so `requirements.txt` never
needed them), and an unset `REVIEW_CONTROL_SECRET` makes `/api/health` return
503 forever, so the entrypoint generates one the same way it generates the
signing key.

**Still open:** the review services are not in the bundle yet (M1b), and
Windows itself is untested — that is the operator's next step, and the only
variable left is whether Docker Desktop's own path translation agrees with the
map.

### M2 — Remote projects, phase one: remote sandbox

* A project gains an optional `host:` (an SSH target).
* For such a project, `run_shell_sandboxed` executes against
  `DOCKER_HOST=ssh://…`. Model commands and checks now run on the remote box.
* Host-side git (`shell.py` → `git.py`) runs over SSH for those projects.

**Done when:** a task on a remote project can read the repo, run its checks and
report results, with nothing installed on the remote box but sshd, Docker and
git.

**Known gap at this point:** `read`/`write`/`edit` still address a local
filesystem, so this phase alone cannot *modify* a remote project. That is M3.

### M3 — Remote projects, phase two: remote files and review

* `files.py` behind the executor: sftp for a remote project, direct I/O for a
  local one.
* The commit-reviewer either runs remotely or drives git remotely.
* Worktree creation and the merge path follow.

**Done when:** a remote project completes the full loop — plan, edit, check,
commit, review, merge — from a control plane on another machine.

**Cost to be honest about:** every `read`/`edit` becomes a network round trip.
Cheaper than a container spawn, but no longer free, and the tools-first nudge
work from 2026-09-12 assumed free.

### M4 — Desktop shell *(optional, last)*

A small Tauri wrapper that starts the stack, shows status and opens the UI. It
is a finish, not a foundation: building it before M1 means shipping an
installer around a stack that still needs manual setup.

---

## Non-goals

* **Native Windows without containers.** WSL2 runs the current code today with
  zero changes, and everything a native port would need is superseded by M1.
* **Mounting a remote filesystem** (sshfs and friends). Git worktrees, the
  sandbox and the checks would still execute on the laptop against a network
  filesystem — slow, and the wrong toolchain. We want remote *execution*, not
  remote files.
* **A hosted multi-tenant edition.** M2/M3 make it possible later; it is not
  what these milestones are for.

---

## Open questions

* Does the socket-mounted sibling-container model hold on Docker Desktop for
  Windows, where the daemon lives in a WSL2 VM and the "host" path is already
  a translation? M1 answers this.
* Postgres in the bundle, or SQLite for a single-user local edition? Postgres
  is one more container but zero code change; SQLite is a smaller install and
  a real port of the checkpointer. Defaulting to Postgres-in-compose until
  someone complains.
* How does a remote project's `.git` pointer file resolve when the worktree
  and the live repo are both remote but the agent is not? Probably fine, since
  both paths are remote and the container is remote too — but it is exactly
  the kind of thing that is fine until it isn't.
