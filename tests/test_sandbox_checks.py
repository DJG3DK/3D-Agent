"""audit C-2: the deterministic check runner executes agent-authored code (npm
scripts + test-writer test files), so it must run inside the Docker sandbox with
network isolation, and must resolve node_modules that a worktree symlinks to the
live checkout."""
import os

import pytest

from agent.tools import sandbox


def test_node_modules_symlink_into_the_live_checkout_is_mounted(tmp_path, monkeypatch):
    """The legitimate case: a worktree whose node_modules symlinks into its own
    project's LIVE checkout still gets that directory mounted read-only."""
    live = tmp_path / "live"
    (live / "node_modules" / "eslint").mkdir(parents=True)
    wt = tmp_path / "worktree"
    wt.mkdir()
    os.symlink(live / "node_modules", wt / "node_modules")
    monkeypatch.setattr("agent.config.PROJECTS",
                        {"p": {"live": str(live), "sandbox": str(wt)}}, raising=False)

    joined = " ".join(sandbox._node_modules_mounts(str(wt)))
    assert str((live / "node_modules").resolve()) in joined
    assert joined.endswith(":ro"), "live node_modules must be mounted read-only"


@pytest.mark.parametrize("escape", ["/", "/root", "/etc", "/home"])
def test_node_modules_symlink_cannot_mount_an_arbitrary_host_path(tmp_path, monkeypatch, escape):
    """audit C2: the symlink lives in the agent-writable worktree, so its target
    is an agent-chosen mount. Pointing it at a host path must NOT produce a
    `-v <path>:<path>:ro` argument — that was read access to every other
    project's .env, this agent's secrets and ~/.ssh."""
    live = tmp_path / "live"
    live.mkdir()
    wt = tmp_path / "worktree"
    wt.mkdir()
    os.symlink(escape, wt / "node_modules")
    monkeypatch.setattr("agent.config.PROJECTS",
                        {"p": {"live": str(live), "sandbox": str(wt)}}, raising=False)

    mounts = sandbox._node_modules_mounts(str(wt))
    assert mounts == [], f"agent-chosen mount of {escape} was accepted: {mounts}"


def test_unconfigured_workspace_mounts_nothing_outside_itself(tmp_path):
    """No server-owned live path to compare against means fail closed."""
    other = tmp_path / "other"
    other.mkdir()
    wt = tmp_path / "worktree"
    wt.mkdir()
    os.symlink(other, wt / "node_modules")
    assert sandbox._node_modules_mounts(str(wt)) == []


def test_mount_target_allowed_accepts_only_configured_roots(tmp_path):
    roots = [str(tmp_path / "ws"), str(tmp_path / "live")]
    (tmp_path / "ws").mkdir()
    (tmp_path / "live" / "sub").mkdir(parents=True)
    assert sandbox._mount_target_allowed(str(tmp_path / "live" / "sub"), roots)
    assert sandbox._mount_target_allowed(str(tmp_path / "ws"), roots)
    assert not sandbox._mount_target_allowed("/root", roots)
    assert not sandbox._mount_target_allowed(str(tmp_path / "elsewhere"), roots)
    # a sibling whose name merely starts with an allowed root must not pass
    assert not sandbox._mount_target_allowed(str(tmp_path / "ws-evil"), roots)


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
