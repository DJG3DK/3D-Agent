"""Covers the build agent's read/write/edit tools on filesystem errors that
are NOT FileNotFoundError.

These used to escape the tool entirely. langgraph's ToolNode re-raises any
exception that isn't a ToolInvocationError, so an escaping OSError doesn't
fail one tool call -- it tears down the whole graph run mid-task. The
concrete trigger was a git worktree's `.git`, which is a one-line pointer
FILE: anything under `.git/` raises NotADirectoryError [Errno 20], and every
project sandbox here is a worktree.
"""

from agent.tools.agent_tools import make_agent_tools


def _tools(tmp_path):
    tools, _ = make_agent_tools(str(tmp_path))
    return {t.name: t for t in tools}


def _worktree_git_pointer(tmp_path) -> None:
    (tmp_path / ".git").write_text("gitdir: /home/my-service/.git/worktrees/my-service\n", encoding="utf-8")


async def test_read_through_a_worktree_git_pointer_returns_an_error(tmp_path):
    _worktree_git_pointer(tmp_path)
    result = await _tools(tmp_path)["read"].ainvoke({"path": ".git/HEAD"})
    assert result.startswith("ERROR:")
    assert "worktree" in result
    # files.py deliberately keeps the host sandbox root out of model-visible text.
    assert str(tmp_path) not in result


async def test_read_on_a_directory_returns_an_error(tmp_path):
    (tmp_path / "src").mkdir()
    result = await _tools(tmp_path)["read"].ainvoke({"path": "src"})
    assert result.startswith("ERROR:")
    assert "directory" in result


def test_write_through_a_worktree_git_pointer_returns_an_error(tmp_path):
    _worktree_git_pointer(tmp_path)
    result = _tools(tmp_path)["write"].invoke({"path": ".git/hooks/pre-commit", "content": "x"})
    assert result.startswith("ERROR:")
    assert str(tmp_path) not in result


def test_write_onto_an_existing_directory_returns_an_error(tmp_path):
    (tmp_path / "src").mkdir()
    result = _tools(tmp_path)["write"].invoke({"path": "src", "content": "x"})
    assert result.startswith("ERROR:")


def test_edit_through_a_worktree_git_pointer_returns_an_error(tmp_path):
    _worktree_git_pointer(tmp_path)
    result = _tools(tmp_path)["edit"].invoke(
        {"path": ".git/HEAD", "old_string": "a", "new_string": "b"}
    )
    assert result.startswith("ERROR:")
    assert str(tmp_path) not in result


async def test_an_unexpected_read_failure_returns_a_string_instead_of_raising(tmp_path, monkeypatch):
    def _explode(*_a, **_kw):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr("agent.tools.agent_tools.read_file", _explode)
    result = await _tools(tmp_path)["read"].ainvoke({"path": "a.txt"})
    assert result.startswith("ERROR:")
    assert "something nobody predicted" in result
