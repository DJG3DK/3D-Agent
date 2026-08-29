"""Regressions from the 2026-08-24 security audit of this repo.

Each of these was a live defect, not a hypothetical. They share a shape:
a guard that existed but did not actually cover the path in question.
"""

import os
import tempfile

import pytest

from agent.tools.agent_tools import make_agent_tools
from agent.tools.files import PathEscapeError, _resolve


# ---------------------------------------------------------------------------
# describe_image escaped the repo entirely
# ---------------------------------------------------------------------------
# read/write/edit all routed through _resolve(), which enforces containment.
# describe_image used os.path.join(repo_root, path) instead -- and os.path.join
# DISCARDS the root when the second argument is absolute. So
# describe_image("/home/3d-agent/.env") read that file off the host and sent
# its contents to the vision model. The mime check did not help: an
# extensionless file guesses to None, which the code defaulted to "image/png",
# so precisely the files worth stealing passed it. This tool runs host-side,
# not in the bash container, so nothing else stood behind it.

def _describe_image_tool(repo_root):
    tools, _ = make_agent_tools(repo_root)
    return next(t for t in tools if t.name == "describe_image")


@pytest.mark.parametrize("escape", [
    "/etc/hostname",
    "/etc/passwd",
    "../../../etc/hostname",
    "../../../../root/.ssh/id_rsa",
])
async def test_describe_image_refuses_to_leave_the_repo(escape):
    with tempfile.TemporaryDirectory() as repo:
        result = await _describe_image_tool(repo).ainvoke({"path": escape})
        assert result.startswith("ERROR")
        # And specifically not because the vision call failed -- it must be
        # refused before any file is read.
        assert "vision call failed" not in result


async def test_describe_image_refuses_unknown_file_types():
    """An extensionless secret used to be treated as image/png by default."""
    with tempfile.TemporaryDirectory() as repo:
        secret = os.path.join(repo, "id_rsa")
        with open(secret, "w") as fh:
            fh.write("-----BEGIN OPENSSH PRIVATE KEY-----")
        result = await _describe_image_tool(repo).ainvoke({"path": "id_rsa"})
        assert result.startswith("ERROR")
        assert "not a recognised image" in result


def test_resolve_still_blocks_the_same_escapes():
    """The shared guard describe_image now uses."""
    with tempfile.TemporaryDirectory() as repo:
        for escape in ("/etc/passwd", "../../etc/passwd"):
            with pytest.raises(PathEscapeError):
                _resolve(repo, escape)
        # ...while ordinary repo-relative paths still resolve.
        assert str(_resolve(repo, "src/App.tsx")).startswith(os.path.realpath(repo))


# ---------------------------------------------------------------------------
# Authorization failed OPEN when a task's repo could not be resolved
# ---------------------------------------------------------------------------
# `_resolve_task_repo` returns None for a missing/unreadable checkpoint. Three
# call sites read `if repo: check_repo_access(...)` -- so an unresolvable repo
# skipped the check entirely and any authenticated user could act on the task.

def test_task_endpoints_fail_closed_on_unresolvable_repo():
    import inspect
    from agent import server

    for fn in (server.send_message, server.stop_task, server.stream_task):
        # Strip comments first -- the fix's own comment quotes the old
        # fail-open line verbatim, which would match a naive substring check.
        src = inspect.getsource(fn)
        code = "\n".join(
            line for line in src.split("\n") if not line.strip().startswith("#")
        )
        assert "_resolve_task_repo" in code, f"{fn.__name__} no longer resolves a repo"
        assert "if repo:" not in code and "if task_repo and" not in code, (
            f"{fn.__name__} reverted to a fail-open access check"
        )
