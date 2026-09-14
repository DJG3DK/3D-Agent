"""How much context a build task keeps, and why it is operator-tunable.

Fixed at 80_000 tokens since the middleware landed. The coder model's actual
window is 1,048,576 -- the trigger was 7.6% of it, and the planning agent had
already been given 170_000 because 80k "turned out to be actively harmful
there".

Live on 2026-09-14, task aa457790: the coder rode that ceiling for an hour.
Context climbed to ~80k, compacted to ~55k, climbed again -- 15 summarizer
calls, 192 tool calls, 0 writes, and the worktree byte-identical at the end of
it. Its two most recent completed plan items were both "Recon: read mtCore.js,
multiTrader.js, mtBacktester.js..." -- it re-read its own output twice, because
each compaction threw away what it had just read.
"""

from __future__ import annotations

import pytest

from agent import deep_agent
from agent import runtime_settings as rs


def test_the_default_is_no_longer_a_sliver_of_the_window():
    """The coder model holds 1,048,576 tokens. A trigger under a tenth of that
    compacts a task that had plenty of room left."""
    assert rs.KNOBS["summarization_trigger_tokens"]["default"] >= 200_000


def test_a_build_gets_at_least_what_planning_gets():
    """Planning was raised to 170k long ago for exactly this reason. A build
    task reads more code than a planning turn, not less."""
    assert rs.KNOBS["summarization_trigger_tokens"]["default"] >= 170_000


def test_keep_is_clamped_below_the_trigger(monkeypatch):
    """The degenerate state: if the kept window approaches the trigger,
    summarization fires before every model call and can never get back under.
    Two independent knobs can reach that by accident."""
    monkeypatch.setitem(rs._values, "summarization_trigger_tokens", 100_000.0)
    monkeypatch.setitem(rs._values, "summarization_keep_tokens", 95_000.0)
    unit, keep = deep_agent.summarization_keep()
    assert unit == "tokens"
    assert keep <= 60_000, "keep must stay well under the trigger"


def test_a_sane_pair_is_left_alone(monkeypatch):
    monkeypatch.setitem(rs._values, "summarization_trigger_tokens", 250_000.0)
    monkeypatch.setitem(rs._values, "summarization_keep_tokens", 90_000.0)
    assert deep_agent.summarization_keep() == ("tokens", 90_000)
    assert deep_agent.summarization_trigger() == [("tokens", 250_000)]


def test_the_units_match(monkeypatch):
    """A message-count keep against a token-count trigger is a unit mismatch
    with nothing bounding how much a compaction reclaims -- it caused
    summarization before every call on task 828ca1d9. Both sides stay tokens."""
    assert deep_agent.summarization_trigger()[0][0] == "tokens"
    assert deep_agent.summarization_keep()[0] == "tokens"


def test_the_knobs_are_read_per_build_not_at_import(monkeypatch):
    """Changing them in Settings has to affect the next work pass, not require
    a restart."""
    monkeypatch.setitem(rs._values, "summarization_trigger_tokens", 300_000.0)
    assert deep_agent.summarization_trigger() == [("tokens", 300_000)]
    monkeypatch.setitem(rs._values, "summarization_trigger_tokens", 220_000.0)
    assert deep_agent.summarization_trigger() == [("tokens", 220_000)]


def test_the_agent_uses_the_functions_not_the_old_constants():
    import inspect
    src = inspect.getsource(deep_agent.build_deep_agent)
    assert "trigger=summarization_trigger()" in src
    assert "keep=summarization_keep()" in src


@pytest.mark.parametrize("knob", ["summarization_trigger_tokens", "summarization_keep_tokens"])
def test_the_knobs_are_bounded(knob):
    spec = rs.KNOBS[knob]
    assert spec["min"] < spec["default"] < spec["max"]
    assert spec["unit"] == "tokens"
    assert spec["group"] == "Context"
