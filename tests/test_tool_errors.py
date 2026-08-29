"""Unit tests for agent/tools/tool_errors.py -- the net that keeps an
unexpected tool exception from tearing down a whole graph run (langgraph's
default ToolNode handler re-raises anything that isn't a ToolInvocationError,
which ends the turn rather than the tool call).
"""

import pytest
from langchain_core.tools import tool
from langgraph.errors import GraphInterrupt

from agent.tools.tool_errors import tool_errors_to_text


def test_sync_exception_becomes_a_tool_result_string():
    @tool_errors_to_text
    def boom(x: str) -> str:
        raise RuntimeError("kaboom")

    result = boom("hi")
    assert result.startswith("ERROR:")
    assert "boom" in result and "RuntimeError" in result and "kaboom" in result


async def test_async_exception_becomes_a_tool_result_string():
    @tool_errors_to_text
    async def boom(x: str) -> str:
        raise ValueError("nope")

    result = await boom("hi")
    assert result.startswith("ERROR:")
    assert "nope" in result


def test_successful_call_passes_through_untouched():
    @tool_errors_to_text
    def fine(x: str) -> str:
        return f"got {x}"

    assert fine("hi") == "got hi"


def test_oserror_message_carries_errno_text_but_not_the_host_path(tmp_path):
    """OSError's str() embeds the absolute host path; files.py deliberately
    keeps the sandbox root out of anything the model sees."""
    not_a_dir = tmp_path / "pkg"
    not_a_dir.write_text("i am a file", encoding="utf-8")

    @tool_errors_to_text
    def read_through_a_file() -> str:
        return (not_a_dir / "HEAD").read_text(encoding="utf-8")

    result = read_through_a_file()
    assert "Not a directory" in result
    assert str(tmp_path) not in result


def test_graph_control_flow_is_re_raised_not_swallowed():
    """GraphInterrupt is how the HITL approval gate (deep_agent.py's
    INTERRUPT_ON) pauses a run -- turning it into an "ERROR" string would
    silently skip the approval prompt instead of showing it."""
    @tool_errors_to_text
    def needs_approval() -> str:
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        needs_approval()


async def test_graph_control_flow_is_re_raised_from_async_tools_too():
    @tool_errors_to_text
    async def needs_approval() -> str:
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        await needs_approval()


async def test_wrapping_preserves_the_schema_langchain_infers():
    """@tool sits ABOVE this decorator, so it builds name/description/args
    from the wrapper -- functools.wraps plus __wrapped__ has to keep all
    three identical to the undecorated function."""
    @tool
    @tool_errors_to_text
    def sample(repo: str, path: str = ".") -> str:
        """Docstring the model actually reads."""
        raise OSError(20, "Not a directory")

    assert sample.name == "sample"
    assert "Docstring the model actually reads." in sample.description
    assert set(sample.args) == {"repo", "path"}
    assert (await sample.ainvoke({"repo": "demo", "path": "x"})).startswith("ERROR:")
