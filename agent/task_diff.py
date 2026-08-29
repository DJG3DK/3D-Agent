"""Live diff of a project workspace against its base branch.

Feeds the dashboard's diff panel two ways:
  * WHILE a task runs -- polled every few seconds, so the operator can watch
    the agent's edits land file by file (committed or not).
  * WHEN a task parks on pending_merge_approval -- the final-look review
    before the operator lets it merge.

Everything here shells out to git inside the workspace root and parses the
output into per-file entries the frontend can shade. No path from the request
ever reaches a command line: the only inputs are the repo name (resolved
through PROJECTS) and git's own output.
"""

from __future__ import annotations

import asyncio
import os

from agent.config import PROJECTS

# A single file's patch beyond this is elided (the header row still shows the
# file and its +/- counts). Keeps one generated lockfile from turning the
# panel into a 2MB payload.
MAX_PATCH_CHARS = 60_000
MAX_UNTRACKED_BYTES = 200_000


# audit M-31: this endpoint is polled every few seconds while a task runs, so a
# hung git process would pile up. Every other subprocess spawn in the codebase
# wraps a wait_for; this one didn't.
_GIT_TIMEOUT_S = 20
# Cap how many untracked files get an individual `git diff --no-index` spawn --
# a stray node_modules / build dir otherwise means thousands of process spawns
# and an unbounded response. Files past the cap are reported, not diffed.
MAX_UNTRACKED_FILES = 200


async def _git(repo_root: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_root, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return 1, f"(git {' '.join(args)[:60]} timed out after {_GIT_TIMEOUT_S}s)"
    return proc.returncode or 0, out.decode(errors="replace")


def split_patch(patch_text: str) -> dict[str, str]:
    """Splits one combined `git diff` output into {path: file_patch}.

    Keyed by the NEW path (b/...) so renames land under the name the reviewer
    will see going forward.
    """
    files: dict[str, str] = {}
    current: list[str] | None = None
    current_path: str | None = None
    for line in patch_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                files[current_path] = "".join(current or [])
            # `diff --git a/x b/x` -- take the b/ side, handling spaces by
            # splitting on ' b/' from the right.
            try:
                current_path = line.rstrip("\n").rsplit(" b/", 1)[1]
            except IndexError:
                current_path = line.rstrip("\n")
            current = [line]
        elif current is not None:
            current.append(line)
    if current_path is not None:
        files[current_path] = "".join(current or [])
    return files


def parse_numstat(numstat_text: str) -> dict[str, tuple[int | None, int | None]]:
    """{path: (additions, deletions)}; None for binary files (git prints '-')."""
    out: dict[str, tuple[int | None, int | None]] = {}
    for line in numstat_text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], parts[-1]
        # rename lines look like "old => new" or "{a => b}/rest" -- keep the
        # resolved new path form git already prints in the last field.
        adds = None if add_s == "-" else int(add_s)
        dels = None if del_s == "-" else int(del_s)
        out[path] = (adds, dels)
    return out


async def collect_task_diff(repo: str, base_ref: str = "main") -> dict:
    """Everything the workspace holds that `base_ref` does not -- committed
    AND uncommitted, plus untracked files -- as one structured payload."""
    project = PROJECTS.get(repo)
    if not project:
        raise KeyError(f"unknown repo {repo!r}")
    root = project["sandbox"]

    rc, head = await _git(root, "rev-parse", "HEAD")
    rc_b, branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")

    # Diff against the BRANCH POINT, not the base branch's tip. The workspace
    # worktree can sit behind main between tasks (it's only fast-forwarded when
    # a task starts), and diffing a behind-HEAD worktree against `main` shows
    # main's own recent history in reverse -- 133 phantom files on an idle
    # workspace when this was first probed. merge-base(HEAD, main) is where
    # this line of work actually forked, so an idle workspace reads as zero
    # and a task branch shows exactly the agent's changes.
    rc_mb, merge_base = await _git(root, "merge-base", "HEAD", base_ref)
    if rc_mb == 0 and merge_base.strip():
        base_ref = merge_base.strip()

    # Working tree (committed + uncommitted) vs base, in one pass each for
    # numstat and patches.
    _, numstat = await _git(root, "diff", "--numstat", base_ref)
    _, patch = await _git(root, "diff", "--patch", "--no-color", base_ref)
    counts = parse_numstat(numstat)
    patches = split_patch(patch)

    files = []
    for path, (adds, dels) in counts.items():
        text = patches.get(path, "")
        truncated = len(text) > MAX_PATCH_CHARS
        files.append({
            "path": path,
            "additions": adds,
            "deletions": dels,
            "binary": adds is None,
            "untracked": False,
            "patch": "" if truncated else text,
            "truncated": truncated,
        })

    # Untracked files are invisible to `git diff <ref>` but are real work the
    # agent produced -- synthesize an all-additions patch for each.
    _, untracked = await _git(root, "ls-files", "--others", "--exclude-standard")
    untracked_paths = untracked.splitlines()
    untracked_overflow = max(0, len(untracked_paths) - MAX_UNTRACKED_FILES)
    for path in untracked_paths[:MAX_UNTRACKED_FILES]:
        full = os.path.join(root, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > MAX_UNTRACKED_BYTES:
            files.append({"path": path, "additions": None, "deletions": None,
                          "binary": False, "untracked": True, "patch": "",
                          "truncated": True})
            continue
        # --no-index exits 1 when the files differ; that's success here.
        _, ptext = await _git(root, "diff", "--no-color", "--no-index", "/dev/null", full)
        adds = sum(1 for line in ptext.splitlines()
                   if line.startswith("+") and not line.startswith("+++"))
        files.append({
            "path": path,
            "additions": adds,
            "deletions": 0,
            "binary": "\x00" in ptext,
            "untracked": True,
            "patch": ptext if len(ptext) <= MAX_PATCH_CHARS else "",
            "truncated": len(ptext) > MAX_PATCH_CHARS,
        })

    files.sort(key=lambda f: f["path"])
    # audit M-31: if untracked files were capped, add one marker entry so the UI
    # can show the diff is incomplete rather than silently dropping them.
    if untracked_overflow:
        files.append({"path": f"({untracked_overflow} more untracked files not shown)",
                      "additions": None, "deletions": None, "binary": False,
                      "untracked": True, "patch": "", "truncated": True})
    return {
        "repo": repo,
        "base": base_ref,
        "head": head.strip() if rc == 0 else None,
        "branch": branch.strip() if rc_b == 0 else None,
        "files": files,
        "total_additions": sum(f["additions"] or 0 for f in files),
        "total_deletions": sum(f["deletions"] or 0 for f in files),
    }
