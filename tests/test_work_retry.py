"""Covers the work-node retry policy: a malformed tool-call from an
underlying model can get rejected by the provider with a 400, surfacing as
openai.BadRequestError. A blanket `except Exception` in work_node would
catch it and escalate the whole task to a human on the very first
occurrence. The fix: work_node re-raises openai.APIError specifically
(everything else still escalates as before), and outer_graph.py attaches a
RetryPolicy to "work" (mirroring the one verify_and_ship already had) so
this class of transient, provider-layer failure gets a fast, automatic
retry -- a real chance of success given the router picks adaptively per
call -- before ever reaching a human.

Drives a REAL compiled LangGraph graph (MemorySaver checkpointer) rather than
calling work_node() bare -- its own docstring explicitly warns
get_stream_writer() requires a runnable context and will raise RuntimeError
outside one.
"""

from functools import partial

import httpx
import openai
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from agent.nodes import work as work_module
from agent.outer_state import AgentState, initial_state


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item


class _FakeRun:
    """Empty run -- no messages/todos, not interrupted. Enough for work_node
    to reach a normal successful return once astream_events stops raising."""

    def __init__(self):
        self.values = _AsyncIter([])
        self.subagents = _AsyncIter([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def interrupted(self):
        return False

    async def interrupts(self):
        return []


class _FakeAgent:
    def __init__(self, fail_times: int, exc_factory):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.calls = 0

    async def astream_events(self, graph_input, config, version):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return _FakeRun()

    async def aget_state(self, config):
        return type("FakeState", (), {"values": {}})()


class _FakeTracker:
    total_cost = 0.0


def _openai_connection_error():
    return openai.APIConnectionError(message="malformed tool call args", request=httpx.Request("POST", "http://test"))


def _build_mini_graph(checkpointer):
    # app_config/checkpointer/pg_store are bound exactly as outer_graph.py
    # binds them in production -- unused by the fake build_deep_agent below,
    # so None is fine for this test, but the WIRING (a partial, RetryPolicy
    # attached the same way) matches real production graph construction.
    builder = StateGraph(AgentState)
    builder.add_node(
        "work",
        partial(work_module.work_node, app_config=None, checkpointer=None, pg_store=None),
        retry_policy=RetryPolicy(max_attempts=3),
    )
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return builder.compile(checkpointer=checkpointer)


async def _run(monkeypatch, fail_times: int, exc_factory=_openai_connection_error):
    fake_agent = _FakeAgent(fail_times=fail_times, exc_factory=exc_factory)
    monkeypatch.setattr(
        work_module, "build_deep_agent",
        lambda *a, **k: _fake_build_deep_agent_result(fake_agent),
    )
    checkpointer = MemorySaver()
    graph = _build_mini_graph(checkpointer)
    state = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=10.0)
    config = {"configurable": {"thread_id": "t1"}}
    return await graph.ainvoke(state, config=config), fake_agent


async def _fake_build_deep_agent_result(fake_agent):
    return fake_agent, _FakeTracker(), {"signature": None}


async def test_transient_api_error_retries_and_recovers(monkeypatch):
    """Fails twice (openai.APIConnectionError), succeeds on the 3rd attempt
    -- within max_attempts=3, so the task must NOT escalate."""
    result, fake_agent = await _run(monkeypatch, fail_times=2)
    assert result["escalated"] is False
    assert fake_agent.calls == 3


async def test_transient_api_error_exhausts_retries_and_escalates(monkeypatch):
    """Fails on every attempt -- retries are exhausted, LangGraph's own
    retry mechanism re-raises the original exception out of ainvoke()
    rather than silently succeeding. This must surface as a real failure the
    caller can see, not a silently-swallowed no-op."""
    with pytest.raises(openai.APIConnectionError):
        await _run(monkeypatch, fail_times=99)


async def test_non_api_error_escalates_immediately_without_retry(monkeypatch):
    """A bug in OUR OWN code (e.g. a ValueError) must still escalate
    immediately, exactly as before this fix -- only openai.APIError gets the
    new retry-then-escalate treatment. Confirms the fix is narrowly scoped,
    not "retry everything.\""""
    result, fake_agent = await _run(monkeypatch, fail_times=1, exc_factory=lambda: ValueError("real bug"))
    assert result["escalated"] is True
    assert "real bug" in result["escalation_reason"]
    assert fake_agent.calls == 1, "must NOT retry a non-API error"
