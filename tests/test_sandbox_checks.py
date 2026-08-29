"""audit C-2: the deterministic check runner executes agent-authored code (npm
scripts + test-writer test files), so it must run inside the Docker sandbox with
network isolation, and must resolve node_modules that a worktree symlinks to the
live checkout."""
import os

import pytest

from agent.tools import sandbox


def test_node_modules_symlink_target_is_mounted(tmp_path):
    # worktree with node_modules -> an out-of-tree "live" install
    live = tmp_path / "live_node_modules"
    (live / "eslint").mkdir(parents=True)
    wt = tmp_path / "worktree"
    wt.mkdir()
    os.symlink(live, wt / "node_modules")

    mounts = sandbox._node_modules_mounts(str(wt))
    joined = " ".join(mounts)
    assert str(live.resolve()) in joined
    assert joined.endswith(":ro"), "live node_modules must be mounted read-only"


def test_real_node_modules_needs_no_extra_mount(tmp_path):
    wt = tmp_path / "worktree"
    (wt / "node_modules" / "eslint").mkdir(parents=True)
    assert sandbox._node_modules_mounts(str(wt)) == []


def test_internal_symlink_is_not_mounted(tmp_path):
    # a symlink that stays INSIDE the worktree (pnpm-store style) is already
    # covered by the /workspace mount and must not add a redundant bind.
    wt = tmp_path / "worktree"
    (wt / ".store" / "eslint").mkdir(parents=True)
    os.symlink(wt / ".store", wt / "node_modules")
    assert sandbox._node_modules_mounts(str(wt)) == []


@pytest.mark.asyncio
async def test_check_runner_requests_network_isolation(monkeypatch, tmp_path):
    # run_check must call the sandbox with network="none".
    (tmp_path / "package.json").write_text('{"scripts": {"lint": "eslint"}}')
    seen = {}

    async def fake_sandboxed(cmd, cwd, timeout=120, extra_env=None, network=None):
        seen.update(cmd=cmd, network=network)
        return {"ok": True, "exit_code": 0, "output": "ok"}

    import agent.tools.checks as checks
    monkeypatch.setattr(checks, "run_shell_sandboxed", fake_sandboxed)
    r = await checks.run_check(str(tmp_path), "lint", timeout=60)
    assert r["ran"] and r["ok"]
    assert seen["network"] == "none"
    assert seen["cmd"] == "npm run lint"
