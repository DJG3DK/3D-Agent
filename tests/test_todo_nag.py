"""StaleTodoMiddleware -- the reminder fires only when the coordinator has
actually stopped maintaining its plan, and says nothing otherwise.

Ground truth for the shape of a stale list: task 4467864c, two hours in,
one `in_progress` and eleven `pending`.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from agent.middleware.todo_nag import NAG_EVERY, StaleTodoMiddleware, render_nag


class _Req:
    """The ModelRequest fields the middleware touches, plus override()."""

    def __init__(self, todos, system="BASE PROMPT", messages=None):
        self.state = {"todos": todos} if todos is not None else {}
        self.system_message = SystemMessage(content=system) if system is not None else None
        self.messages = list(messages) if messages else [HumanMessage(content="the goal")]

    def override(self, system_message=None, messages=None):
        r = _Req(None)
        r.state = self.state
        r.system_message = system_message if system_message is not None else self.system_message
        r.messages = list(messages) if messages is not None else list(self.messages)
        return r


def _seen_by_model(mw, req):
    """One model turn: (system text, trailing message text) as sent."""
    seen = {}

    def handler(r):
        seen["system"] = r.system_message.text if r.system_message is not None else ""
        seen["tail"] = r.messages[-1].text if r.messages else ""
        seen["count"] = len(r.messages)
        return "response"

    mw.wrap_model_call(req, handler)
    return seen


def _system_after(mw, req):
    """What the model saw as its SYSTEM prompt. The nag must never be here:
    prompt caching keys on a byte-identical prefix, and this is the prefix."""
    return _seen_by_model(mw, req)["system"]


def _nag_after(mw, req):
    """What the model saw at the END of the conversation -- where the nag
    belongs, because it costs no cached prefix there."""
    return _seen_by_model(mw, req)["tail"]


PLAN = [{"status": "in_progress", "content": "Read all target files"},
        {"status": "pending", "content": "Group 1+2 (api): html-entities.ts"}]


def test_a_list_that_keeps_moving_is_never_nagged():
    mw = StaleTodoMiddleware(every=3)
    for i in range(NAG_EVERY * 2):
        plan = [{"status": "completed" if j <= i else "pending", "content": f"item {j}"} for j in range(50)]
        assert "PLAN CHECK" not in _nag_after(mw, _Req(plan))


def test_the_reminder_arrives_only_after_the_list_sits_still():
    mw = StaleTodoMiddleware(every=3)
    assert "PLAN CHECK" not in _nag_after(mw, _Req(PLAN)), "first sighting is not staleness"
    assert "PLAN CHECK" not in _nag_after(mw, _Req(PLAN))
    assert "PLAN CHECK" not in _nag_after(mw, _Req(PLAN))
    assert "PLAN CHECK" in _nag_after(mw, _Req(PLAN)), "three unchanged turns later"


def test_it_names_the_next_unfinished_item():
    mw = StaleTodoMiddleware(every=1)
    _system_after(mw, _Req(PLAN))
    text = _nag_after(mw, _Req(PLAN))
    assert "Read all target files" in text, "the in_progress item is the one to talk about"
    assert "2 item(s) outstanding" in text


def test_the_system_prompt_is_never_touched():
    """Prompt caching keys on a byte-identical prefix, so editing the system
    message is the most expensive thing this middleware could do -- at 70-80k
    tokens of prefix it invalidates the cached context for that whole call.
    The nag is a trailing message instead."""
    mw = StaleTodoMiddleware(every=1)
    _seen_by_model(mw, _Req(PLAN))
    seen = _seen_by_model(mw, _Req(PLAN))
    assert "PLAN CHECK" in seen["tail"], "it still reaches the model"
    assert seen["system"] == "BASE PROMPT", "and the cached prefix is untouched"


def test_it_is_added_to_the_conversation_not_a_replacement_of_it():
    mw = StaleTodoMiddleware(every=1)
    _seen_by_model(mw, _Req(PLAN))
    seen = _seen_by_model(mw, _Req(PLAN, messages=[HumanMessage(content="the goal"),
                                                   HumanMessage(content="a tool result")]))
    assert seen["count"] == 3, "appended, never replacing what was there"


def test_a_turn_that_does_not_nag_leaves_the_messages_alone():
    mw = StaleTodoMiddleware(every=3)
    seen = _seen_by_model(mw, _Req(PLAN, messages=[HumanMessage(content="the goal")]))
    assert seen["count"] == 1 and "PLAN CHECK" not in seen["tail"]


def test_updating_the_list_resets_the_count():
    mw = StaleTodoMiddleware(every=2)
    _system_after(mw, _Req(PLAN))
    _system_after(mw, _Req(PLAN))
    moved = [{"status": "completed", "content": "Read all target files"},
             {"status": "in_progress", "content": "Group 1+2 (api): html-entities.ts"}]
    assert "PLAN CHECK" not in _nag_after(mw, _Req(moved))
    assert "PLAN CHECK" not in _nag_after(mw, _Req(moved)), "the clock restarts from the change"
    assert "PLAN CHECK" in _nag_after(mw, _Req(moved))


def test_a_finished_list_is_left_alone():
    done = [{"status": "completed", "content": "a"}, {"status": "completed", "content": "b"}]
    mw = StaleTodoMiddleware(every=1)
    for _ in range(4):
        assert "PLAN CHECK" not in _nag_after(mw, _Req(done))


def test_no_plan_at_all_is_not_nagged():
    """A short task that never calls write_todos is exactly the case the tool
    description tells the model to skip -- it must not be badgered into one."""
    mw = StaleTodoMiddleware(every=1)
    for _ in range(4):
        assert "PLAN CHECK" not in _nag_after(mw, _Req(None))


def test_the_nag_repeats_on_the_interval_not_on_every_turn():
    mw = StaleTodoMiddleware(every=2)
    fired = [bool("PLAN CHECK" in _nag_after(mw, _Req(PLAN))) for _ in range(9)]
    # turn 0 is the first sighting; the counter ticks from turn 1
    assert fired == [False, False, True, False, True, False, True, False, True]


def test_render_nag_is_empty_when_everything_is_ticked():
    assert render_nag([{"status": "completed", "content": "a"}], 99) == ""
