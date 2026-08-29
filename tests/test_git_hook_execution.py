"""C1: host code execution through a rewritten .git pointer and a git hook.

Every git command in agent/tools/git.py runs ON THE HOST, with cwd set to a
worktree the agent can write to. A worktree's `.git` is a one-line pointer
FILE, so the agent could point it at a git dir it had just created inside the
workspace, complete with a pre-commit hook — and the host-side commit would
execute that hook as the server user (root under the shipped pm2 config).

The write was ungated too: the approval gate matched ".git/" WITH a slash,
which the bare pointer file ".git" does not contain.

These tests run the real git_commit() against a real worktree with a real
hook installed. If the hook ever executes, it leaves a file behind and the
test fails — this asserts the exploit's effect, not the presence of a flag.
"""

import os
import subprocess
from pathlib import Path

import pytest

from agent.tools import git as gitmod


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def live_and_worktree(tmp_path):
    """A live repo plus a real git worktree of it — the shape a task runs in."""
    live = tmp_path / "live"
    live.mkdir()
    _run(["git", "init", "-q", "-b", "main"], live)
    _run(["git", "config", "user.email", "t@t.t"], live)
    _run(["git", "config", "user.name", "t"], live)
    (live / "f.txt").write_text("one\n")
    _run(["git", "add", "-A"], live)
    _run(["git", "commit", "-qm", "init"], live)

    wt = tmp_path / "workspace"
    _run(["git", "worktree", "add", "-q", str(wt), "-b", "agent/task-1"], live)
    assert (wt / ".git").is_file(), "a worktree's .git must be a pointer file"
    return live, wt


def _plant_hook(root: Path, marker: Path) -> Path:
    """Create an agent-controlled git dir with a pre-commit hook, and point the
    worktree's .git at it — the exact two writes the agent can make."""
    evil = root / "notdotgit"          # deliberately avoids the ".git/" substring
    (evil / "hooks").mkdir(parents=True)
    (evil / "objects").mkdir()
    (evil / "refs").mkdir()
    (evil / "HEAD").write_text("ref: refs/heads/main\n")
    hook = evil / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (root / ".git").write_text(f"gitdir: {evil}\n")
    return hook


async def test_commit_does_not_execute_a_hook_from_a_rewritten_pointer(live_and_worktree, tmp_path):
    live, wt = live_and_worktree
    marker = tmp_path / "HOOK_RAN_ON_HOST"
    _plant_hook(wt, marker)

    (wt / "f.txt").write_text("two\n")
    await gitmod.git_commit(str(wt), "attempt")

    assert not marker.exists(), (
        "the pre-commit hook executed on the host — this is remote code execution "
        "as the server user, which runs as root under the shipped pm2 config")


async def test_hooks_are_disabled_even_for_a_legitimate_repo(tmp_path):
    """Hooks are disabled unconditionally, not only when the pointer looks odd —
    a hook committed to the repo itself is equally unreviewed code."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@t.t"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    marker = tmp_path / "PLAIN_HOOK_RAN"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    (repo / "a.txt").write_text("x\n")
    res = await gitmod.git_commit(str(repo), "msg")

    assert res["ok"], res["output"]
    assert not marker.exists(), "a repo-local pre-commit hook still executed"


async def test_checkout_paths_also_disable_hooks(live_and_worktree, tmp_path):
    """checkout/merge run hooks too, so the flag can't live only on commit."""
    live, wt = live_and_worktree
    marker = tmp_path / "POST_CHECKOUT_RAN"
    evil = wt / "notdotgit"
    (evil / "hooks").mkdir(parents=True)
    (evil / "HEAD").write_text("ref: refs/heads/main\n")
    hook = evil / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (wt / ".git").write_text(f"gitdir: {evil}\n")

    await gitmod.ensure_task_branch(str(wt), "task-2")
    assert not marker.exists(), "post-checkout hook executed on the host"


def test_a_rewritten_pointer_is_rejected_for_a_configured_project(tmp_path, monkeypatch):
    """Beyond hooks: a rewritten pointer can aim commits at another repository.
    When the workspace belongs to a configured project, that is refused."""
    live = tmp_path / "live"
    (live / ".git").mkdir(parents=True)
    wt = tmp_path / "ws"
    wt.mkdir()
    monkeypatch.setattr("agent.config.PROJECTS",
                        {"p": {"live": str(live), "sandbox": str(wt)}}, raising=False)

    (wt / ".git").write_text(f"gitdir: {live}/.git/worktrees/ws\n")
    assert gitmod._trusted_git_dir_error(str(wt)) is None, "the legitimate pointer must pass"

    (wt / ".git").write_text(f"gitdir: {tmp_path}/elsewhere\n")
    err = gitmod._trusted_git_dir_error(str(wt))
    assert err and "outside" in err


def test_unconfigured_workspaces_are_not_broken_by_the_check(tmp_path):
    """One-off checkouts and test fixtures have no server-owned path to compare
    against; they must still work, since hooks are already disabled for them."""
    repo = tmp_path / "loose"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /somewhere/else\n")
    assert gitmod._trusted_git_dir_error(str(repo)) is None
