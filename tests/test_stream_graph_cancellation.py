"""Regression test: _stream_graph's cancellation handler must record the
task's real cost at the moment of cancellation, not a stale checkpoint
value. The outer checkpoint's own cost_so_far only updates when a work pass
fully returns, so cancelling mid-pass (exactly what the operator's Stop
button does) would otherwise silently under-report -- and, worse, leave a
stale low value in the checkpoint that a future resume's BudgetTracker
would start counting from, corrupting the budget ceiling.
"""

import asyncio

import pytest

import agent.server as srv


class _FakeItem:
    def __init__(self, value):
        self.value = value


class _FakeStore:
    def __init__(self, initial_meta):
        self.data = {("tasks", "test-repo"): {"t1": dict(initial_meta)}}

    async def aget(self, namespace, key):
        val = self.data.get(namespace, {}).get(key)
        return _FakeItem(val) if val is not None else None

    async def aput(self, namespace, key, value):
        self.data.setdefault(namespace, {})[key] = dict(value)


class _FakeGraph:
    def __init__(self, events):
        self._events = events
        self.updated_state = {}

    async def astream(self, graph_input, thread_config, stream_mode, durability):
        for ev in self._events:
            if ev == "CANCEL":
                raise asyncio.CancelledError()
            yield ev

    async def aget_state(self, thread_config):
        return None  # not exercised on this path (graph_input is not None)

    async def aupdate_state(self, thread_config, patch):
        self.updated_state.update(patch)


async def test_cancellation_records_live_cost_not_stale_checkpoint(monkeypatch):
    store = _FakeStore({
        "task_id": "t1", "goal": "g", "repo": "test-repo",
        "budget_usd": 6.0, "status": "running", "cost_so_far": 0.0,
    })
    # Two live "cost" custom events (as work.py's tracker would emit
    # mid-pass), then a cancellation -- simulating the Stop button
    # interrupting an actively-running, actively-spending pass.
    events = [
        ("custom", {"type": "cost", "cost_so_far": 3.00}),
        ("custom", {"type": "cost", "cost_so_far": 6.01}),
        "CANCEL",
    ]
    graph = _FakeGraph(events)

    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "graph", graph, raising=False)

    # _stream_graph re-raises CancelledError after handling it, so the
    # /stop endpoint's own `task.cancel(); await task` sees it correctly.
    with pytest.raises(asyncio.CancelledError):
        await srv._stream_graph("t1", "test-repo", "g", 6.0, {"messages": []})

    stored = store.data[("tasks", "test-repo")]["t1"]
    assert stored["status"] == "stopped"
    assert stored["cost_so_far"] == 6.01, "must record the live-tracked cost, not a stale/zero checkpoint value"
    assert graph.updated_state.get("cost_so_far") == 6.01, (
        "the checkpoint itself must be patched so a resumed task's BudgetTracker starts from the real total"
    )
