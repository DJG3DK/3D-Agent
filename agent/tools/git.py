import re
import asyncio
from agent.tools.shell import run_shell


async def git_status(repo_root: str) -> str:
    r = await run_shell("git status --short", repo_root, timeout=30)
    return r["output"]


async def git_diff(repo_root: str, staged: bool = False) -> str:
    """Includes newly created (untracked) files, not just changes to tracked
    ones. Plain `git diff` silently omits untracked files entirely, which
    would make a genuinely new file look like no change happened at all.
    `git add -N` (intent-to-add: stages the path, not the content) makes new
    files show up in `git diff` normally without actually staging their
    content -- this is not intended as a real `git add`.
    """
    await run_shell("git add -A -N", repo_root, timeout=30)
    cmd = "git diff --staged" if staged else "git diff"
    r = await run_shell(cmd, repo_root, timeout=30)
    return r["output"]


async def sync_workspace_to_base(repo_root: str, base_ref: str = "main") -> dict:
    """Move an IDLE workspace to the current live tip before a fresh task edits.

    Why this exists: the workspace worktree only ever sat where the previous
    task left it, while direct pushes land on live main continuously. A fresh
    task then edited a stale tree, and ensure_task_branch -- which runs at
    COMMIT time, when the agent's own uncommitted edits have already dirtied
    the tree -- took its dirty-tree branch-from-HEAD path every single time,
    silently pinning the task branch to the stale base. First observed live
    2026-08-26: a task branched 9 commits behind main, its (review-approved,
    operator-approved) merge then failed --ff-only with "diverging branches",
    and the agent had spent the whole task editing week-old code.

    Only acts on a CLEAN tree -- a dirty tree means a resumed or concurrent
    task owns the workspace, and moving the base under real work is exactly
    the kind of silent damage this module exists to prevent. Detached
    checkout, because `main` itself is checked out in the live worktree and
    git (correctly) refuses to check a branch out twice.
    """
    status = await run_shell("git status --porcelain", repo_root, timeout=15)
    if not status["ok"]:
        return {"ok": False, "synced": False, "reason": f"status failed: {status['output'][:200]}"}
    if status["output"].strip():
        return {"ok": True, "synced": False, "reason": "tree dirty -- workspace belongs to in-flight work"}
    r = await run_shell(f"git checkout --detach {base_ref}", repo_root, timeout=30)
    if not r["ok"]:
        return {"ok": False, "synced": False, "reason": r["output"][:300]}
    sha = await run_shell("git rev-parse --short HEAD", repo_root, timeout=15)
    return {"ok": True, "synced": True, "base": sha["output"].strip() if sha["ok"] else base_ref}


async def ensure_task_branch(repo_root: str, task_id: str, base_ref: str = "main") -> dict:
    """Put the agent's workspace on a per-task branch before committing.

    The workspace is a git worktree of the live repo, so this branch is created
    directly in live's own object store -- there is no clone to keep in sync and
    no remote to fetch through. The reviewer and the merge endpoint read the
    branch as a plain local ref.

    Two things this protects against, both observed:
      * committing to `main` made one ref serve two writers (the agent, and the
        refresh cron that fast-forwarded it), and
      * without a branch there was no stable review unit, so the reviewer
        inferred one by comparing HEADs -- which produced inverted diffs and two
        false `blocking` findings on live trading safety settings.

    Idempotent: on resume the branch already exists and is checked out, and this
    returns without touching the tree.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(task_id)).strip("-.") or "task"
    branch = f"agent/{safe}"

    cur = await run_shell("git rev-parse --abbrev-ref HEAD", repo_root, timeout=15)
    if cur["ok"] and cur["output"].strip() == branch:
        return {"ok": True, "branch": branch, "switched": False}

    # A fresh task should start from the current live tip rather than from
    # wherever the previous task left the workspace. Only safe when the tree is
    # clean -- if there is uncommitted work, branch from HEAD so it survives,
    # and say so rather than discarding it.
    status = await run_shell("git status --porcelain", repo_root, timeout=15)
    dirty = bool(status["ok"] and status["output"].strip())

    if dirty:
        cmd = f"git checkout -B {branch}"
    else:
        cmd = f"git checkout -B {branch} {base_ref}"

    r = await run_shell(cmd, repo_root, timeout=30)
    if not r["ok"] and not dirty:
        # base_ref may not exist (unusual default branch name); fall back to HEAD.
        r = await run_shell(f"git checkout -B {branch}", repo_root, timeout=30)

    return {
        "ok": r["ok"],
        "branch": branch,
        "switched": True,
        "from_base": not dirty,
        "output": r["output"],
    }


# audit M-12: git_commit used `git add -A`, and the sandboxed bash can create
# arbitrary files in the worktree -- every one landed in the commit that goes to
# review and, on approval, into the live repo (core.excludesFile only covers
# .uploads/). These directories/suffixes are never legitimate commit content;
# their presence in the pending set is a build/dep artifact that must be cleaned
# (or gitignored) before committing, not silently shipped.
_COMMIT_DENY_DIRS = (
    "node_modules/", "dist/", "build/", ".next/", ".venv/", "venv/",
    "__pycache__/", ".pytest_cache/", "coverage/", ".mypy_cache/",
    "target/", ".turbo/", ".cache/",
)
_COMMIT_DENY_SUFFIXES = (".pyc", ".log", ".tmp")
_MAX_COMMIT_FILES = 500


def _porcelain_paths(porcelain: str) -> list[str]:
    paths = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # rename entries are "old -> new"; take the destination
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


async def git_commit(repo_root: str, message: str, files: list[str] | None = None) -> dict:
    if not files:
        # audit M-12: guard the blanket `git add -A`. Inspect what would be
        # staged and refuse obvious artifacts / a runaway file count, so the
        # agent cleans up rather than committing junk into the live repo.
        status = await run_shell("git status --porcelain", repo_root, timeout=30)
        if status["ok"]:
            pending = _porcelain_paths(status["output"])
            denied = [pth for pth in pending
                      if any(d in pth for d in _COMMIT_DENY_DIRS)
                      or pth.endswith(_COMMIT_DENY_SUFFIXES)]
            if denied:
                sample = ", ".join(denied[:10])
                return {"ok": False, "output": (
                    f"refusing to commit {len(denied)} build/dependency artifact(s) that "
                    f"should be gitignored or removed first: {sample}"
                    + (" ..." if len(denied) > 10 else ""))}
            if len(pending) > _MAX_COMMIT_FILES:
                return {"ok": False, "output": (
                    f"refusing to commit {len(pending)} files at once (limit {_MAX_COMMIT_FILES}) -- "
                    "this usually means an un-ignored directory got created; clean it up or "
                    "pass an explicit file list.")}
    add_cmd = f"git add {' '.join(files)}" if files else "git add -A"
    add = await run_shell(add_cmd, repo_root, timeout=30)
    if not add["ok"]:
        return {"ok": False, "output": add["output"]}
    # Message via a temp file, not -m "...", so multi-line messages with
    # quotes/special chars can never break the shell invocation.
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(message)
        msg_path = f.name
    try:
        commit = await run_shell(f"git commit -F {msg_path}", repo_root, timeout=30)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    return {"ok": commit["ok"], "output": commit["output"]}


async def current_sha(repo_root: str) -> str:
    r = await run_shell("git rev-parse HEAD", repo_root, timeout=15)
    return r["output"].strip()


async def sha_in_repo(repo_root: str, sha: str) -> bool:
    """True iff `sha` is an ancestor of (or equal to) `repo_root`'s HEAD --
    used by verify_and_ship to detect that the review service's
    auto-merge-on-READY already merged the pending commit to the live repo
    (the two are separate paths to the same merge and can race, which would
    otherwise nudge an already-finished task for a verdict that auto-merge
    had already consumed).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_root, "merge-base", "--is-ancestor", sha, "HEAD",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0
