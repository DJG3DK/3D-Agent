"""The run registries are claimed atomically.

`_running_tasks` and `_running_planning_turns` were plain dicts guarded by
`if key in registry: raise 409` at the TOP of a handler, with the real
assignment several awaits later. The event loop switches at every await, so
two concurrent requests could both pass the check and both create a driver
for the same thread -- and the unconditional `registry.pop(key)` in the
driver's `finally` then orphaned whichever one survived.

A reservation placed with no await between the check and the write is atomic
here, which is what `_claim_run_slot` does.
"""

import asyncio

import pytest
from fastapi import HTTPException

from agent.server import _claim_run_slot


def test_a_second_claim_on_the_same_key_is_refused():
    reg: dict = {}
    with _claim_run_slot(reg, "t1", "already running"):
        with pytest.raises(HTTPException) as exc:
            with _claim_run_slot(reg, "t1", "already running"):
                pass
        assert exc.value.status_code == 409
        reg["t1"] = object()   # the handler assigns its real task


def test_two_concurrent_claimants_only_one_wins():
    """The actual race: both coroutines await between claiming and assigning."""
    reg: dict = {}
    winners, losers = [], []

    async def handler(name):
        try:
            with _claim_run_slot(reg, "same-task", "already running"):
                await asyncio.sleep(0)      # the window the bug lived in
                await asyncio.sleep(0)
                reg["same-task"] = f"task-{name}"
                winners.append(name)
        except HTTPException:
            losers.append(name)

    async def main():
        await asyncio.gather(handler("a"), handler("b"))

    asyncio.run(main())
    assert len(winners) == 1, f"both claimants started: {winners}"
    assert len(losers) == 1


def test_the_slot_is_released_when_the_handler_raises():
    """A 404 or 403 after the claim must not strand the key -- otherwise the
    task could never be started again."""
    reg: dict = {}
    with pytest.raises(HTTPException):
        with _claim_run_slot(reg, "t1", "already running"):
            raise HTTPException(404, "task not found")
    assert "t1" not in reg, "a rejected request left a phantom claim"

    # and the key is genuinely reusable afterwards
    with _claim_run_slot(reg, "t1", "already running"):
        reg["t1"] = object()
    assert reg["t1"] is not None


def test_the_slot_is_released_if_the_handler_never_assigns():
    """A handler that returns early without creating a task must not hold the
    slot forever either."""
    reg: dict = {}
    with _claim_run_slot(reg, "t1", "already running"):
        pass
    assert "t1" not in reg


def test_a_real_assignment_is_kept():
    reg: dict = {}
    sentinel = object()
    with _claim_run_slot(reg, "t1", "already running"):
        reg["t1"] = sentinel
    assert reg["t1"] is sentinel


def test_cancellation_also_releases():
    """BaseException, not just Exception: a CancelledError during the claim
    window must not leave the key held."""
    reg: dict = {}
    with pytest.raises(asyncio.CancelledError):
        with _claim_run_slot(reg, "t1", "already running"):
            raise asyncio.CancelledError()
    assert "t1" not in reg


def test_consumers_read_the_reservation_as_not_running():
    """stop_task and stop_planning_turn do `registry.get(key)` and treat a
    falsy value as 'nothing to cancel'. During the reservation window that is
    the correct answer -- the task genuinely has not started."""
    reg: dict = {}
    with _claim_run_slot(reg, "t1", "already running"):
        assert reg.get("t1") is None, "a placeholder must be falsy for .get() consumers"
        reg["t1"] = object()
