"""The planning turn is bounded by silence, not by duration.

A flat wall-clock ceiling kills the turns most worth running: a hard question
that is actively reading files and calling tools looks identical, to a timer,
to one that has hung. On 2026-08-30 a live turn was killed at 30 minutes while
still streaming, having spent $2.50 and saved no plan.

These pin the replacement: a long turn that keeps producing output is left
alone, and only genuine silence is cut off.
"""

import asyncio

import pytest

from agent.server import PlanningStalled, _await_with_stall_watchdog


async def _run(coro_fn, heartbeat, stall_s):
    task = asyncio.create_task(coro_fn())
    return await _await_with_stall_watchdog(task, heartbeat, stall_s)


@pytest.mark.asyncio
async def test_a_long_turn_that_keeps_talking_is_never_cut_off():
    """The whole point: duration alone must not end a turn."""
    beat = {"at": asyncio.get_running_loop().time(), "events": 0}

    async def chatty():
        # Far more elapsed time than the stall window, but never silent for
        # longer than it -- exactly the shape of a hard planning turn.
        import time as _t
        for _ in range(12):
            await asyncio.sleep(0.02)
            beat["at"] = _t.monotonic()
            beat["events"] += 1
        return "# a plan"

    import time as _t
    beat["at"] = _t.monotonic()
    assert await _run(chatty, beat, stall_s=0.10) == "# a plan"
    assert beat["events"] == 12


@pytest.mark.asyncio
async def test_silence_past_the_window_is_cut_off():
    import time as _t
    beat = {"at": _t.monotonic(), "events": 3}

    async def hung():
        await asyncio.sleep(30)  # never speaks again
        return "unreachable"

    with pytest.raises(PlanningStalled) as exc:
        await _run(hung, beat, stall_s=0.05)
    # The message has to say what happened, or the Telegram alert is useless.
    assert "no output" in str(exc.value)
    assert "3 events" in str(exc.value)


@pytest.mark.asyncio
async def test_a_stall_is_not_reported_as_operator_cancellation():
    """PlanningStalled must NOT be CancelledError.

    The caller's `except asyncio.CancelledError` branch means "the operator
    pressed Stop". If a stall arrived as CancelledError it would be filed as a
    deliberate stop, and the failure would never alert.
    """
    import time as _t
    beat = {"at": _t.monotonic(), "events": 0}

    async def hung():
        await asyncio.sleep(30)

    with pytest.raises(PlanningStalled) as exc:
        await _run(hung, beat, stall_s=0.05)
    assert not isinstance(exc.value, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_the_underlying_turn_is_actually_cancelled_on_stall():
    """A stall must stop the work, not leak a task that keeps spending."""
    import time as _t
    beat = {"at": _t.monotonic(), "events": 0}
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hung():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(PlanningStalled):
        await _run(hung, beat, stall_s=0.05)
    assert started.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_an_error_inside_the_turn_propagates_unchanged():
    """The watchdog must not mask a real failure as a stall."""
    import time as _t
    beat = {"at": _t.monotonic(), "events": 1}

    async def boom():
        raise ValueError("model refused")

    with pytest.raises(ValueError, match="model refused"):
        await _run(boom, beat, stall_s=5)


@pytest.mark.asyncio
async def test_a_fast_turn_returns_immediately():
    import time as _t
    beat = {"at": _t.monotonic(), "events": 0}

    async def quick():
        return "# done"

    assert await _run(quick, beat, stall_s=5) == "# done"
