"""Regression test for a summarization trim bug:
SummarizationMiddleware's default trim_tokens_to_summarize=4000 produced the
literal fallback string "Previous conversation was too long to summarize."
mid-task, wiping the goal and reviewer feedback from context. Root cause:
_trim_messages_for_summary uses trim_messages(strategy="last",
start_on="human"), which requires a HumanMessage inside the trim budget --
our conversations are tool-call-heavy (one early HumanMessage, then dozens of
AIMessage/ToolMessage pairs), so any finite budget can miss the sole
HumanMessage if the tool-heavy stretch after it is long enough (confirmed:
raising the budget to 20_000 still hit the bug). The actual fix disables the
trim step entirely (trim_tokens_to_summarize=None) -- safe because
SUMMARIZATION_TRIGGER already bounds the untrimmed batch to roughly its own
token threshold, well inside any modern model's real context window.
"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain.agents.middleware.summarization import SummarizationMiddleware

from agent.deep_agent import SUMMARIZATION_TRIM_TOKENS


def _toolcall_heavy_history(n_pairs: int) -> list:
    """One early HumanMessage (the goal), then n_pairs of AIMessage+ToolMessage
    with realistic-sized content -- no other HumanMessage anywhere, matching
    a long work pass with no loop-back in between.
    """
    messages = [HumanMessage(content="Refactor the request handler. " * 20)]
    filler = "some tool output content that is moderately long. " * 60  # ~500 tokens/msg
    for i in range(n_pairs):
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "bash", "args": {"command": f"cmd {i}"}, "id": f"call_{i}"}],
            )
        )
        messages.append(ToolMessage(content=filler, tool_call_id=f"call_{i}"))
    return messages


def _make_middleware(trim_tokens_to_summarize) -> SummarizationMiddleware:
    return SummarizationMiddleware(
        model=FakeListChatModel(responses=["a real summary"]),
        trim_tokens_to_summarize=trim_tokens_to_summarize,
    )


def test_old_default_can_produce_empty_trim_on_toolcall_heavy_history():
    """Confirms the bug is real against the actual library code, not a
    misreading -- the library's own default (4000) empties out on this shape."""
    middleware = _make_middleware(trim_tokens_to_summarize=4000)
    history = _toolcall_heavy_history(n_pairs=60)  # matches the bug's real message count
    trimmed = middleware._trim_messages_for_summary(history)
    summary = middleware._create_summary(history)
    assert trimmed == [] or summary == "Previous conversation was too long to summarize."


def test_a_larger_finite_budget_is_not_actually_safe():
    """A naive fix (just raise the number) doesn't work -- documents why
    SUMMARIZATION_TRIM_TOKENS is None instead of some larger constant. If this
    test ever starts failing, trim_messages' start_on="human" behavior changed
    upstream and the None-based fix should be re-evaluated."""
    middleware = _make_middleware(trim_tokens_to_summarize=20_000)
    history = _toolcall_heavy_history(n_pairs=60)
    trimmed = middleware._trim_messages_for_summary(history)
    assert trimmed == []  # still empty even at 5x the original budget


def test_configured_fix_avoids_the_fallback():
    """The actual fix: SUMMARIZATION_TRIM_TOKENS=None skips the trim step
    entirely, so there's no budget to run out of room in."""
    assert SUMMARIZATION_TRIM_TOKENS is None
    middleware = _make_middleware(trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS)
    history = _toolcall_heavy_history(n_pairs=60)
    trimmed = middleware._trim_messages_for_summary(history)
    assert trimmed == history  # untrimmed -- None means "don't trim"
    summary = middleware._create_summary(history)
    assert summary != "Previous conversation was too long to summarize."
    assert summary == "a real summary"


def test_configured_fix_holds_on_a_much_larger_history():
    """No budget to exhaust means no size-dependent ceiling -- check well
    beyond the original failing size to confirm this isn't just a bigger
    version of the same finite-budget mistake."""
    middleware = _make_middleware(trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS)
    history = _toolcall_heavy_history(n_pairs=300)
    trimmed = middleware._trim_messages_for_summary(history)
    assert trimmed == history
    summary = middleware._create_summary(history)
    assert summary != "Previous conversation was too long to summarize."


def test_planning_chat_wires_the_same_fix():
    """Confirmed live (2026-08-23): agent/planning_chat.py's own
    SummarizationMiddleware call omitted trim_tokens_to_summarize entirely,
    silently falling back to the library default (4000) -- exactly this
    bug, live, in production. A planning session opens with one long
    HumanMessage (the operator's own detailed problem statement) followed by
    a long tool-heavy stretch of repo reads -- the identical shape this
    whole file documents -- and the failed summarization's fallback string
    silently destroyed the original ask, with no error surfaced anywhere;
    the model then had no idea what it had been asked and asked the
    operator to restate a problem they'd just described in detail. Guards
    against ever losing this fix silently again by asserting the actual
    source wires it, since building a real planning agent needs a live
    Postgres store/checkpointer this test suite deliberately never touches.
    """
    import inspect

    from agent.planning_chat import build_planning_agent

    source = inspect.getsource(build_planning_agent)
    assert "trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS" in source
