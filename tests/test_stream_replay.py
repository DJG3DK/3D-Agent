"""One tool result, one log entry -- however many projections can see it.

Every projection of a run (`run.values` for the coordinator, `handle.values`
for each subagent) reports its WHOLE accumulated message list on every
superstep, not a delta. So whether a message gets published once or many times
is decided entirely by the already-seen set the consumer was handed.

It used to be handed a fresh one per consumer. Measured on task 279c29fd
(2026-09-12): 1328 of 1864 logged tool results were republications, the same
burst reappearing verbatim up to three times -- `{bash: 74, write: 3,
write_todos: 2}` at 17:59, 18:38 and 18:46. Every one of those also went to
the dashboard as a live log entry, which is what "the same commands are being
spammed" looked like in the task view, and it inflated every count on the
Analytics tool panel. The ratio gave it away: 4.7 tool results per model call,
where deduplicating brings it to 1.4.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent import tool_events
from agent.nodes import work


class _Projection:
    """A run.values-shaped async iterable: the whole accumulated list, every
    superstep, exactly as langgraph reports it."""

    def __init__(self, *snapshots):
        self._snapshots = snapshots

    def __aiter__(self):
        async def gen():
            for s in self._snapshots:
                yield s
        return gen()


def _tool_result(i: int) -> ToolMessage:
    return ToolMessage(content=f"exit_code=0\nout {i}", tool_call_id=f"c{i}", name="bash", id=f"m{i}")


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_events, "LOG_PATH", tmp_path / "tool_events.jsonl")
    entries: list[dict] = []
    return entries, (lambda e: entries.append(e))


def test_one_projection_publishes_each_message_once(published):
    entries, writer = published
    history = [_tool_result(0), _tool_result(1)]
    proj = _Projection({"messages": history[:1]}, {"messages": history})

    asyncio.run(work._consume_values("T1", "work", proj, writer, {}))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 2, "the accumulated list must not be re-emitted each superstep"


def test_a_second_consumer_does_not_republish_what_the_first_saw(published):
    """The actual bug: a subagent handle arrives mid-run and its consumer can
    see history the root consumer already published."""
    entries, writer = published
    history = [_tool_result(i) for i in range(5)]
    shared: set = set()

    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": history}),
                                     writer, {}, seen_ids=shared))
    asyncio.run(work._consume_values("T1", "work:investigator", _Projection({"messages": history}),
                                     writer, {}, seen_ids=shared))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 5, f"5 results, 5 entries -- got {len(logs)}"


def test_without_the_shared_set_the_history_is_replayed(published):
    """Pins the mechanism, so nobody 'simplifies' the shared set away: given
    its own set, the second consumer republishes everything."""
    entries, writer = published
    history = [_tool_result(i) for i in range(5)]

    for _ in range(2):
        asyncio.run(work._consume_values("T1", "work", _Projection({"messages": history}),
                                         writer, {}))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 10, "this is the replay the shared set exists to prevent"


def test_the_tool_log_is_written_once_per_result_too(tmp_path, monkeypatch):
    """The same bug inflated the Analytics panel, not just the live view."""
    log = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(tool_events, "LOG_PATH", log)
    history = [_tool_result(i) for i in range(3)]
    shared: set = set()

    for label in ("work", "work:investigator", "work:test-writer"):
        asyncio.run(work._consume_values("T1", label, _Projection({"messages": history}),
                                         lambda _e: None, {}, seen_ids=shared))

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 3, f"3 tool results across 3 projections, {len(rows)} rows"
    assert {r["tool"] for r in rows} == {"bash"}


def test_an_assistant_turn_is_published_once_as_well(published):
    """Not specific to tool results -- the model's own turns were duplicated
    in the live view by the same path."""
    entries, writer = published
    history = [AIMessage(content="thinking about it", id="a1")]
    shared: set = set()

    for label in ("work", "work:investigator"):
        asyncio.run(work._consume_values("T1", label, _Projection({"messages": history}),
                                         writer, {}, seen_ids=shared))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 1


def test_todos_stay_per_projection(published):
    """The shared set covers message ids only. Todos are the coordinator's and
    are compared per projection -- a subagent must not suppress the root's."""
    entries, writer = published
    todos = [{"content": "a", "status": "pending"}]
    asyncio.run(work._consume_values("T1", "work", _Projection({"todos": todos}), writer, {}))
    assert [e["type"] for e in entries] == ["todos"]
