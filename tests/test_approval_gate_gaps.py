"""H2: the approval gate's substring markers left the C1/C2 setup ungated.

Both exploits needed writes that matched no marker:

  * the worktree's `.git` POINTER FILE — the list matched ".git/" with a
    slash, which the bare file ".git" does not contain;
  * a hooks directory under any name that avoids ".git/";
  * `ln -s <host path> node_modules`, which decides what the host bind-mounts
    into the next container.

These assert the predicates directly, since that is where the decision is
made.
"""

import pytest

import agent.deep_agent as da


class _Req:
    def __init__(self, **args):
        self.tool_call = {"args": args}


@pytest.mark.parametrize("path", [
    ".git",                        # the pointer file — the C1 write
    "src/.git",
    "/workspace/.git",
    ".git/hooks/pre-commit",       # already covered before, must stay covered
    "notdotgit/hooks/pre-commit",  # a git dir under a name avoiding ".git/"
    "myhooks/hooks/post-checkout",
    ".env",
    "config/keys.json",
    "package.json",
])
def test_sensitive_writes_prompt(path):
    assert da._file_op_needs_approval(_Req(path=path)), f"{path} should prompt"


@pytest.mark.parametrize("path", [
    "src/app.py", "README.md", "frontend/src/main.tsx", "tests/test_x.py",
])
def test_ordinary_writes_do_not_prompt(path):
    """The gate has to stay quiet on normal work, or operators turn it off."""
    assert not da._file_op_needs_approval(_Req(path=path)), f"{path} should not prompt"


@pytest.mark.parametrize("cmd", [
    "ln -s /home node_modules",
    "ln -sf / x",
    "ln -sfn /root a",
    "cd /workspace && ln -s /etc n",
    "ln --symbolic /root x",
])
def test_symlink_creation_prompts(cmd):
    """A symlink the agent chose the target for is what turned an agent write
    into a host mount (audit C2)."""
    assert da._bash_needs_approval(_Req(command=cmd)), f"{cmd!r} should prompt"


@pytest.mark.parametrize("cmd", [
    "npm ci",
    "echo aligned -symbolic",
    "node -e \"console.log('ln -s')\"",
    "python -m pytest -q",
])
def test_ordinary_commands_do_not_prompt(cmd):
    assert not da._bash_needs_approval(_Req(command=cmd)), f"{cmd!r} should not prompt"


def test_destructive_set_is_unchanged_by_the_symlink_addition():
    """Symlink creation is *sensitive*, not destructive: it should prompt in
    strict mode but must not be forced through the auto-approve ceiling, which
    is reserved for actions a diff revert cannot undo."""
    assert not da._bash_is_destructive(_Req(command="ln -s /home node_modules"))
    assert da._bash_is_destructive(_Req(command="rm -rf /"))
