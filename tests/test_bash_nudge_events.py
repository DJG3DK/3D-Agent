"""One flagged bash call is one bash call.

The harness prepends a note when a shell command was really a file edit, a
file read, or a reach into the agent's own memory (agent/tools/bash_advice.py).
That note used to be accompanied by a second tool event named "bash-as-read",
which meant the Analytics reliability panel listed a tool nobody has and
counted one extra call for every flagged command.

The note now travels on the result, and the work node -- which already writes
exactly one event per tool result -- reads the kind back off it. These tests
run the real bash tool against a fake sandbox and the real translator against
a real ToolMessage, so the two halves are checked where they actually meet.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import ToolMessage

from agent import tool_events
from agent.nodes import work
from agent.tools import agent_tools, bash_advice


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(tool_events, "LOG_PATH", path)
    return path


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _bash(tmp_path, monkeypatch, output="exit_code=0\n"):
    """The real bash tool, with the container replaced by a canned result."""
    async def _fake_sandbox(cmd, cwd, timeout=None, extra_env=None, network=None):
        return {"ok": True, "exit_code": 0, "output": output}

    monkeypatch.setattr(agent_tools, "run_shell_sandboxed", _fake_sandbox)
    tools, _ = agent_tools.make_agent_tools(str(tmp_path))
    return {t.name: t for t in tools}["bash"]


def test_the_tool_writes_no_event_of_its_own(tmp_path, monkeypatch, log):
    """The wrapper's job is the note. The event belongs to the work node, or
    the same call gets logged twice."""
    bash = _bash(tmp_path, monkeypatch)
    result = asyncio.run(bash.ainvoke({"command": "cat src/app.ts"}))
    assert result.startswith(bash_advice.READ_NOTE)
    assert _rows(log) == []


def test_the_work_node_tags_the_event_from_the_result(tmp_path, monkeypatch, log):
    bash = _bash(tmp_path, monkeypatch)
    result = asyncio.run(bash.ainvoke({"command": "grep -n x /memories/AGENTS.md"}))

    work._translate_message("T1", "work", ToolMessage(content=result, tool_call_id="c1", name="bash"))

    rows = _rows(log)
    assert len(rows) == 1, "one tool result, one row"
    assert rows[0]["tool"] == "bash", "not a tool of its own"
    assert rows[0]["nudge"] == "memory-read"
    assert rows[0]["ok"] is True, "the command ran; it was just the expensive way"


def test_an_ordinary_command_is_recorded_with_no_nudge(tmp_path, monkeypatch, log):
    bash = _bash(tmp_path, monkeypatch, output="exit_code=0\n2 passed\n")
    result = asyncio.run(bash.ainvoke({"command": "pytest -q"}))
    assert not result.startswith("[harness]")

    work._translate_message("T1", "work", ToolMessage(content=result, tool_call_id="c1", name="bash"))

    rows = _rows(log)
    assert len(rows) == 1
    assert rows[0]["nudge"] is None


def test_a_failed_tool_result_still_records_its_reason(tmp_path, monkeypatch, log):
    """The nudge field must not have displaced the failure path."""
    msg = ToolMessage(content="Error: String not found in file: 'x'", tool_call_id="c1",
                      name="edit_file", status="error")
    work._translate_message("T1", "work", msg)

    rows = _rows(log)
    assert rows[0]["ok"] is False
    assert "String not found" in rows[0]["detail"]
    assert rows[0]["nudge"] is None


def test_the_memory_write_note_reaches_the_model_on_the_result(tmp_path, monkeypatch, log):
    """The case that silently lost work: the shell reports success, so the
    note is the only thing telling the model its memory was not saved."""
    bash = _bash(tmp_path, monkeypatch)
    result = asyncio.run(bash.ainvoke({"command": "cat >> /memories/AGENTS.md <<'X'\nnote\nX"}))
    assert result.startswith(bash_advice.MEMORY_WRITE_NOTE)
    assert "write_file" in result
