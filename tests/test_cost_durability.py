"""A task's spend has to survive the process that was spending it.

Live, 2026-09-12: a task displayed $0.76 after spending about $9. Four work
passes had been killed by pm2's memory watchdog, and `cost_so_far` lives in the
outer checkpoint -- which is written when a pass RETURNS. Each resume started
again from the last completed boundary, so every killed pass took its spend
with it. The money was real; the ceiling was being enforced against a number
that had rolled back.

The router knew all along: it bills every call and writes a line per call. What
it could not do was total a TASK, because nothing said which task a call
belonged to. So each call now carries the task id, and a pass reconciles
against the ledger before it starts spending.
"""

from __future__ import annotations

import json

from agent.deep_agent import _call_metadata
from agent.nodes.work import _reconciled_cost
from agent.tools.router_ledger import RouterLedger


def _log(tmp_path, rows):
    p = tmp_path / "routing.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def test_every_call_carries_the_task_it_is_for():
    assert _call_metadata("task-1", None) == {"metadata": {"agent_task_id": "task-1"}}
    assert _call_metadata("t", "s") == {"metadata": {"agent_task_id": "t", "agent_session_id": "s"}}


def test_no_task_id_sends_no_metadata():
    """A call outside a task (a probe, the classifier) must not invent one."""
    assert _call_metadata(None, None) is None


def test_the_ledger_totals_a_task_across_passes(tmp_path):
    path = _log(tmp_path, [
        {"call_id": "a", "task_id": "T", "cost": 1.25},
        {"call_id": "b", "task_id": "OTHER", "cost": 99.0},
        {"call_id": "c", "task_id": "T", "cost": 2.50},
        {"call_id": "d", "task_id": "T", "cost": 0.25},
    ])
    assert RouterLedger(path).total_for_task("T") == 4.0
    assert RouterLedger(path).total_for_task("OTHER") == 99.0
    assert RouterLedger(path).total_for_task("missing") == 0.0
    assert RouterLedger(path).total_for_task("") == 0.0


def test_the_total_reads_past_the_tail(tmp_path):
    """A task runs for hours; its early calls are long past the last 512KB the
    per-call lookup reads. This is the one read that scans the whole file."""
    rows = [{"call_id": f"pad{i}", "task_id": "NOISE", "cost": 0.001,
             "detail": "x" * 400} for i in range(3000)]
    rows.insert(0, {"call_id": "first", "task_id": "T", "cost": 7.0})
    rows.append({"call_id": "last", "task_id": "T", "cost": 1.0})
    path = _log(tmp_path, rows)
    assert path.stat().st_size > 600_000, "the fixture has to exceed the tail window"
    assert RouterLedger(path).total_for_task("T") == 8.0


def test_a_torn_line_does_not_lose_the_total(tmp_path):
    path = tmp_path / "routing.jsonl"
    path.write_text(json.dumps({"call_id": "a", "task_id": "T", "cost": 1.0}) + "\n"
                    + '{"call_id": "half", "task_id": "T", "cos\n'
                    + json.dumps({"call_id": "b", "task_id": "T", "cost": 2.0}) + "\n")
    assert RouterLedger(path).total_for_task("T") == 3.0


def test_a_missing_log_totals_zero_rather_than_raising(tmp_path):
    assert RouterLedger(tmp_path / "nope.jsonl").total_for_task("T") == 0.0


# ---------------------------------------------------------------------------
# reconciliation: the larger of the two wins
# ---------------------------------------------------------------------------

def _state(cost, task_id="T"):
    return {"cost_so_far": cost, "task_id": task_id}


def test_a_rolled_back_checkpoint_is_corrected_from_the_ledger(monkeypatch):
    """The live shape: the checkpoint says $0.76, the router has billed $9.12."""
    monkeypatch.setattr(RouterLedger, "total_for_task", lambda self, t: 9.12)
    assert _reconciled_cost(_state(0.76)) == 9.12


def test_the_ledger_never_lowers_the_figure(monkeypatch):
    """The log is trimmed past 5MB, so an old task's early calls can be gone.
    It is a floor, never a correction downward."""
    monkeypatch.setattr(RouterLedger, "total_for_task", lambda self, t: 2.0)
    assert _reconciled_cost(_state(8.0)) == 8.0


def test_small_differences_are_left_alone(monkeypatch):
    """A call in flight is billed but not yet checkpointed; half a cent of
    disagreement is the normal state, not a correction."""
    monkeypatch.setattr(RouterLedger, "total_for_task", lambda self, t: 1.002)
    assert _reconciled_cost(_state(1.0)) == 1.0


def test_a_broken_ledger_never_stops_a_pass(monkeypatch):
    def boom(self, t):
        raise OSError("disk gone")

    monkeypatch.setattr(RouterLedger, "total_for_task", boom)
    assert _reconciled_cost(_state(3.0)) == 3.0


def test_a_task_with_no_id_uses_what_it_has(monkeypatch):
    monkeypatch.setattr(RouterLedger, "total_for_task",
                        lambda self, t: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert _reconciled_cost({"cost_so_far": 4.0}) == 4.0
