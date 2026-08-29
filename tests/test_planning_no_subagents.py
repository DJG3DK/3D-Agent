"""The planning chat must not be able to delegate.

create_deep_agent does NOT treat `subagents=None` as "no subagents": its graph
assembly unconditionally inserts a general-purpose spec carrying the
coordinator's own tools and model, and any non-empty subagent list builds a
`task` tool. planning_chat.py is written as one flat research conversation --
it says so twice, passes no `subagents=`, and its prompt enumerates every tool
without mentioning `task` -- so it got a delegation primitive it never
designed for and never described.

The model used it anyway. A planning turn on 2026-08-27 ran 115 model calls
and 4.7M input tokens against the 1800s ceiling and timed out, with only ~22
tool calls in its own transcript; the rest ran inside nested agent loops. The
auto-added subagent also carries no BudgetGuardMiddleware (that must be
attached per spec by hand), so the turn recorded $0.44 against $3.98 of real
router spend.
"""

import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

import agent.planning_chat as pc
from agent.middleware.hidden_tools import HiddenToolsMiddleware


class _RecordingModel(FakeListChatModel):
    """Captures the tool names actually offered to the model."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        # FakeListChatModel has no bind_tools implementation, so record and
        # return self -- the graph only needs something that yields a message.
        _RecordingModel.seen = [
            (t.get("name") or (t.get("function") or {}).get("name"))
            if isinstance(t, dict) else getattr(t, "name", None)
            for t in tools
        ]
        return self


# ---------------------------------------------------------------------------
# the middleware itself
# ---------------------------------------------------------------------------


def _request(tools):
    from langchain.agents.middleware.types import ModelRequest
    return ModelRequest(model=FakeListChatModel(responses=["x"]), messages=[], tools=list(tools))


class _T:
    def __init__(self, name): self.name = name


def test_hides_the_named_tool():
    mw = HiddenToolsMiddleware("task")
    seen = {}
    mw.wrap_model_call(_request([_T("task"), _T("read_project_file")]),
                       lambda r: seen.update(names=[t.name for t in r.tools]))
    assert seen["names"] == ["read_project_file"]


async def test_hides_it_on_the_async_path_too():
    mw = HiddenToolsMiddleware("task")
    seen = {}

    async def handler(r):
        seen["names"] = [t.name for t in r.tools]

    await mw.awrap_model_call(_request([_T("task"), _T("save_plan")]), handler)
    assert seen["names"] == ["save_plan"]


def test_hides_tools_given_in_openai_dict_form():
    mw = HiddenToolsMiddleware("task")
    seen = {}
    mw.wrap_model_call(
        _request([{"type": "function", "function": {"name": "task"}}, {"name": "grep"}]),
        lambda r: seen.update(n=len(r.tools)),
    )
    assert seen["n"] == 1


def test_leaves_the_original_request_untouched():
    """override() is immutable by contract -- middleware must not mutate a
    request other middleware in the chain still holds."""
    mw = HiddenToolsMiddleware("task")
    req = _request([_T("task"), _T("grep")])
    mw.wrap_model_call(req, lambda r: None)
    assert [t.name for t in req.tools] == ["task", "grep"]


def test_refuses_to_be_constructed_with_nothing_to_hide():
    with pytest.raises(ValueError):
        HiddenToolsMiddleware()


# ---------------------------------------------------------------------------
# the real planning agent
# ---------------------------------------------------------------------------


async def _tools_offered_to_the_planning_model(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "PROJECTS", {"demo": {"sandbox": str(tmp_path)}})
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", {"demo": {"sandbox": str(tmp_path)}})
    monkeypatch.setattr(pc, "llm_for_role", lambda *a, **k: _RecordingModel(responses=["done"]))
    agent, _, _ = await pc.build_planning_agent(
        pc.load_config() if hasattr(pc, "load_config") else __import__("agent.config", fromlist=["load_config"]).load_config(),
        "demo", InMemorySaver(), InMemoryStore(),
    )
    from langchain_core.messages import HumanMessage
    await agent.ainvoke({"messages": [HumanMessage("hello")]},
                        {"configurable": {"thread_id": "t1"}})
    return _RecordingModel.seen


async def test_the_planning_model_is_never_offered_task(monkeypatch, tmp_path):
    offered = await _tools_offered_to_the_planning_model(monkeypatch, tmp_path)
    assert offered, "the recording model saw no bind_tools call at all"
    assert "task" not in offered, f"planning agent still offers delegation: {offered}"


async def test_the_planning_model_still_gets_its_real_tools(monkeypatch, tmp_path):
    """Guard against 'fixing' this by hiding too much. read_file stays because
    it is how the model reads the codebase map and its own memory."""
    offered = await _tools_offered_to_the_planning_model(monkeypatch, tmp_path)
    for expected in ("read_project_file", "list_project_dir", "save_plan", "web_search", "read_file"):
        assert expected in offered, f"{expected} went missing: {offered}"


async def test_the_planning_model_is_never_offered_the_scratch_search_tools(monkeypatch, tmp_path):
    """grep/glob search the SCRATCH space, never the repo. A live session
    (2026-08-27) burned 8 consecutive identical grep('runBacktest',
    'src/core/backtester...') calls -- "No matches found" every time --
    against a path the tool cannot see, despite an explicit prompt warning.
    The trap is removed rather than warned about: the model cannot loop on a
    tool it is never offered. execute (no sandbox behind it here) and delete
    (nothing it should ever delete) go for the same reason."""
    offered = await _tools_offered_to_the_planning_model(monkeypatch, tmp_path)
    for trap in ("grep", "glob", "execute", "delete"):
        assert trap not in offered, f"planning agent still offers the scratch-space trap {trap!r}: {offered}"


def test_the_prompt_teaches_the_codebase_map():
    """Hiding grep only helps if the model has a better way to find things:
    the map read must be the FIRST move of a code investigation."""
    assert "codebase-map/SKILL.md" in pc.PLANNING_SYSTEM_PROMPT
    assert "{skills_summary}" in pc.PLANNING_SYSTEM_PROMPT


def test_the_prompt_tells_the_model_it_cannot_delegate():
    """Removing the tool without saying so leaves the model looking for it."""
    assert "NO way to delegate" in pc.PLANNING_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# hidden tools must be inert at EXECUTION time too
# ---------------------------------------------------------------------------
#
# A model resumed over a history full of its own grep calls can pattern-
# complete another one without the schema offering it; the ToolNode still has
# the tool registered, so it would execute. wrap_tool_call turns that into an
# instructive refusal.


class _ToolCallReq:
    def __init__(self, name, call_id="c1"):
        self.tool_call = {"name": name, "args": {}, "id": call_id}


def test_a_hidden_tool_call_is_refused_not_executed():
    mw = HiddenToolsMiddleware("grep")
    executed = []
    result = mw.wrap_tool_call(_ToolCallReq("grep"), lambda r: executed.append(r))
    assert executed == []
    assert result.status == "error"
    assert result.tool_call_id == "c1"
    assert "bash" in result.content and "grep" in result.content


async def test_a_hidden_tool_call_is_refused_on_the_async_path_too():
    mw = HiddenToolsMiddleware("grep", "glob")
    async def handler(r):
        raise AssertionError("must not execute")
    result = await mw.awrap_tool_call(_ToolCallReq("glob", "c9"), handler)
    assert result.status == "error" and result.tool_call_id == "c9"


def test_a_visible_tool_call_passes_through_to_the_handler():
    mw = HiddenToolsMiddleware("grep")
    result = mw.wrap_tool_call(_ToolCallReq("read_project_file"), lambda r: "ran")
    assert result == "ran"
