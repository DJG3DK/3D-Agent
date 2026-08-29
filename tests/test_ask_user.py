"""ask_user -- the clarification channel that lets the agent ask the
operator a question instead of guessing on an ambiguous goal. The tool must
always interrupt with only the respond decision allowed, and the interrupt
description must carry the actual question so the dashboard can render it."""

from agent.deep_agent import INTERRUPT_ON, _make_ask_user_tool


def test_ask_user_interrupt_allows_only_respond():
    cfg = INTERRUPT_ON["ask_user"]
    assert cfg["allowed_decisions"] == ["respond"]
    assert "when" not in cfg, "every ask_user call must interrupt -- no predicate"


def test_interrupt_description_carries_question_and_options():
    desc = INTERRUPT_ON["ask_user"]["description"](
        {"args": {"question": "Paginate or optimize caching?", "options": "A) paginate\nB) caching"}}, None, None
    )
    assert "Paginate or optimize caching?" in desc
    assert "A) paginate" in desc


def test_tool_fallback_never_pretends_to_have_an_answer():
    result = _make_ask_user_tool().invoke({"question": "which one?"})
    assert "no operator response" in result
    assert "assumption" in result


def test_dangerous_tools_unchanged():
    # ask_user must not have loosened the existing approval gates.
    for name in ("bash", "write", "edit"):
        assert INTERRUPT_ON[name]["allowed_decisions"] == ["approve", "reject"]
        assert "when" in INTERRUPT_ON[name]


def test_description_callable_has_middleware_signature():
    """A single-arg lambda would crash the work node the moment ask_user
    fired -- the middleware calls description(tool_call, state, runtime)."""
    from agent.deep_agent import _describe_ask_user
    desc = _describe_ask_user({"args": {"question": "Q?", "options": "A/B"}}, None, None)
    assert "Q?" in desc and "A/B" in desc
