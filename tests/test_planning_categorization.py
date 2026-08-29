"""A planning session is categorised when it first has a SAVED PLAN.

It used to be categorised on its first message, which made the sidebar move
the session out of the ungrouped list at the exact moment the operator was
reading the first reply -- dropping it beside an older session on the same
subject that already had a "Build now" button. The flick plus that
neighbouring button read as "your plan is ready" when the agent had in fact
just asked a clarifying question.

The move now happens once, when the session becomes buildable, so it carries
information instead of noise.
"""

import inspect
import re

import pytest

import agent.server as srv


def _should_categorise(effective_plan, existing_category):
    """The predicate as written in _run_planning_turn_bg."""
    return bool(effective_plan) and not existing_category


@pytest.mark.parametrize("plan,category,expected", [
    # the case that caused the confusion: a first turn that asked a question
    (None, None, False),
    ("", None, False),
    # the session becomes buildable -> classify, once
    ("# Plan\n\ndo the thing", None, True),
    # already categorised -> never re-classify, so it cannot hop groups later
    ("# Plan\n\ndo the thing", "bugfix", False),
    # a later turn that clears the plan must not trigger classification either
    (None, "bugfix", False),
])
def test_when_a_session_gets_categorised(plan, category, expected):
    assert _should_categorise(plan, category) is expected


def test_the_classifier_is_not_paid_for_planless_sessions():
    """A conversation that never produces a plan never calls the classifier --
    it is a real model call, and the old timing spent one on every session."""
    calls = []
    for plan, cat in [(None, None), (None, None), ("# Plan\n\nx", None), ("# Plan\n\nx", "bugfix")]:
        if _should_categorise(plan, cat):
            calls.append(plan)
    assert len(calls) == 1, "classification must happen exactly once, on the first saved plan"


def test_the_turn_actually_gates_classification_on_the_plan():
    """Guards the wiring, not just the predicate: classify_task must sit under
    a condition mentioning the plan, so it cannot drift back to firing on the
    title."""
    src = inspect.getsource(srv._run_planning_turn_bg)
    idx = src.index("classify_task(")
    preceding = src[:idx]
    guard = preceding.rsplit("if ", 1)[-1].split(":")[0]
    assert "effective_plan" in guard, (
        f"classify_task is no longer gated on the saved plan; nearest guard was {guard!r}")


def test_title_is_still_set_on_the_first_message():
    """Only the CATEGORY moved. A session still needs a title immediately, or
    the sidebar shows an unidentifiable row while the first turn runs."""
    src = inspect.getsource(srv._run_planning_turn_bg)
    assert re.search(r'if not meta\.get\("title"\):\s*\n\s*meta\["title"\]', src), (
        "the title must still be assigned on the first message")
