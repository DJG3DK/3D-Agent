"""Auto-resume of restart-orphaned tasks (server startup).

resume_task's docstring declares restarts routine, and its orphan branch
resumes losslessly -- but nothing called it automatically, so every deploy
stranded in-flight builds until a human noticed the silence (2026-08-27: the
operator watched a stalled screener build and asked why). Startup now
reconnects orphans itself; these tests pin who gets resumed and who must not.
"""

import asyncio

import pytest

import agent.server as server


class _Item:
    def __init__(self, key, value):
        self.key, self.value = key, value


class _FakeStore:
    def __init__(self, tasks):
        self.tasks = tasks   # list[dict]
        self.puts = []

    async def asearch(self, ns, limit=50):
        return [_Item(t["task_id"], dict(t)) for t in self.tasks]

    async def aput(self, ns, key, value):
        self.puts.append((key, dict(value)))


class _Ckpt:
    def __init__(self, values, ckpt_id="ck-1"):
        self.values = values
        self.config = {"configurable": {"checkpoint_id": ckpt_id}}


class _FakeGraph:
    def __init__(self, checkpoints):
        self.checkpoints = checkpoints  # task_id -> _Ckpt | None
        self.patches = []

    async def aget_state(self, cfg):
        return self.checkpoints.get(cfg["configurable"]["thread_id"])

    async def aupdate_state(self, cfg, patch, **kw):
        self.patches.append((cfg["configurable"]["thread_id"], dict(patch)))


class _FakeInnerCp:
    """Fakes the raw checkpointer for the inner-thread progress read."""

    def __init__(self, inner_ts_by_thread=None):
        self.inner = inner_ts_by_thread or {}

    async def aget_tuple(self, cfg):
        ts = self.inner.get(cfg["configurable"]["thread_id"])
        if ts is None:
            return None
        class _Snap:
            checkpoint = {"ts": ts}
        return _Snap()


@pytest.fixture
def wired(monkeypatch):
    started = []

    async def fake_stream_graph(task_id, repo, goal, budget, x):
        started.append(task_id)

    monkeypatch.setattr(server, "_stream_graph", fake_stream_graph)
    monkeypatch.setattr(server, "PROJECTS", {"demo": {}})
    monkeypatch.setattr(server, "_running_tasks", {})

    def wire(tasks, checkpoints, inner=None):
        store = _FakeStore(tasks)
        graph = _FakeGraph(checkpoints)
        monkeypatch.setattr(server.app.state, "store", store, raising=False)
        monkeypatch.setattr(server.app.state, "graph", graph, raising=False)
        monkeypatch.setattr(server.app.state, "checkpointer", _FakeInnerCp(inner), raising=False)
        return store, graph, started

    return wire


def _task(task_id, status="running", **extra):
    return {"task_id": task_id, "status": status, "repo": "demo",
            "goal": "g", "budget_usd": 5.0, **extra}


def _values(escalated=False):
    return {"repo": "demo", "goal": "g", "budget_usd": 5.0,
            "max_iterations": 40, "escalated": escalated}


async def test_an_orphaned_running_task_is_reconnected(wired):
    store, graph, started = wired([_task("t1")], {"t1": _Ckpt(_values())})
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)  # let the scheduled driver coroutine run
    assert started == ["t1"]
    assert graph.patches and graph.patches[0][1]["max_iterations"] == 80
    assert graph.patches[0][1]["budget_usd"] == 5.0, "auto-resume must never add budget"
    assert "t1" in server._running_tasks


async def test_the_resume_is_recorded_so_a_no_progress_loop_stops(wired):
    store, graph, started = wired([_task("t1")], {"t1": _Ckpt(_values(), "ck-9")})
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)  # let the scheduled driver coroutine run
    assert store.puts and store.puts[0][1]["auto_resume_ckpt"] == "ck-9|"


async def test_a_task_with_no_progress_since_last_auto_resume_is_left_alone(wired):
    """Poison-task guard: resuming it did nothing once -- a boot loop of
    retries would amplify the failure, not fix it."""
    store, graph, started = wired(
        [_task("t1", auto_resume_ckpt="ck-9|")], {"t1": _Ckpt(_values(), "ck-9")})
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)  # let the scheduled driver coroutine run
    assert started == []


async def test_progress_since_the_last_auto_resume_allows_another(wired):
    store, graph, started = wired(
        [_task("t1", auto_resume_ckpt="ck-9|")], {"t1": _Ckpt(_values(), "ck-10")})
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)  # let the scheduled driver coroutine run
    assert started == ["t1"]


async def test_escalated_stopped_done_and_driven_tasks_are_not_touched(wired):
    store, graph, started = wired(
        [_task("t-esc"), _task("t-stopped", status="stopped"),
         _task("t-done", status="done"), _task("t-driven")],
        {"t-esc": _Ckpt(_values(escalated=True)), "t-driven": _Ckpt(_values())})
    server._running_tasks["t-driven"] = object()
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)  # let the scheduled driver coroutine run
    assert started == []
    assert graph.patches == []


async def test_one_broken_task_does_not_strand_the_rest(wired):
    class _BrokenCkptGraph(_FakeGraph):
        async def aget_state(self, cfg):
            if cfg["configurable"]["thread_id"] == "t-bad":
                raise RuntimeError("corrupt checkpoint")
            return await super().aget_state(cfg)

    store, graph, started = wired(
        [_task("t-bad"), _task("t-good")], {"t-good": _Ckpt(_values())})
    broken = _BrokenCkptGraph(graph.checkpoints)
    server.app.state.graph = broken
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)  # let the scheduled driver coroutine run
    assert started == ["t-good"]


async def test_inner_thread_progress_counts_as_progress(wired):
    """The guard bug this replaces: the OUTER checkpoint stays frozen for an
    entire work pass, so two restarts inside one long pass looked like "no
    progress" and wrongly stranded a healthy task. Inner-thread checkpoints
    move every step -- progress there must allow another auto-resume even
    with the outer id unchanged."""
    store, graph, started = wired(
        [_task("t1", auto_resume_ckpt="ck-9|10:00:00")],
        {"t1": _Ckpt(_values(), "ck-9")},
        inner={"t1:work": "10:07:33"},
    )
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)
    assert started == ["t1"]
    assert store.puts[0][1]["auto_resume_ckpt"] == "ck-9|10:07:33"


async def test_no_progress_on_either_thread_still_blocks(wired):
    store, graph, started = wired(
        [_task("t1", auto_resume_ckpt="ck-9|10:00:00")],
        {"t1": _Ckpt(_values(), "ck-9")},
        inner={"t1:work": "10:00:00"},
    )
    await server._auto_resume_orphaned_tasks(startup_delay=0)
    await asyncio.sleep(0)
    assert started == []
