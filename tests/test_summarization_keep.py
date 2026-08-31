"""Summarization must RECLAIM a bounded amount of context, not an arbitrary one.

SUMMARIZATION_KEEP was ("messages", 20) while SUMMARIZATION_TRIGGER is
measured in tokens. Nothing related the two, so how much a summarization
actually bought depended entirely on how big the most recent tool results
happened to be -- and our conversations are tool-call-heavy, with inline file
reads of ~10k tokens each.

Measured on a real planning session (2026-08-27) at the state that fired
summarization -- 28 messages / 73_068 tokens, six largest ~10k each -- keeping
20 messages preserved 61_695 tokens. It reclaimed 15% and left 18k of headroom
under an 80k trigger, so it re-fired about two file reads later. The session's
observed cycle was summarize / 2-3 tool calls / summarize.

The degenerate case is worse than churn: once the kept window exceeds the
trigger on its own, summarization fires before every model call and can never
get back under it, so the conversation stops progressing while still paying
for a summarizer call every turn.
"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain.agents.middleware.summarization import SummarizationMiddleware

from agent.deep_agent import SUMMARIZATION_KEEP, SUMMARIZATION_TRIGGER, SUMMARIZATION_TRIM_TOKENS

TRIGGER_TOKENS = next(v for (k, v) in SUMMARIZATION_TRIGGER if k == "tokens")


def _big_read(i: int) -> str:
    """A ToolMessage the size of a real inline file read. Planning's
    read_project_file caps a read at 40_000 chars (~10k tokens), which is
    exactly the size of the six largest messages in the session that fired
    the summarization this test file is about."""
    body = "const someIdentifier = computeSomething(a, b, c);\n" * 800
    return f"// file {i}\n" + body


def _file_heavy_history(n_reads: int) -> list:
    msgs = [HumanMessage(content="Debug why the screener disagrees with the strategy. " * 20)]
    for i in range(n_reads):
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "read_project_file", "args": {"repo": "r", "path": f"f{i}.js"}, "id": f"c{i}"}
        ]))
        msgs.append(ToolMessage(content=_big_read(i), tool_call_id=f"c{i}"))
    return msgs


def _middleware(keep):
    return SummarizationMiddleware(
        model=FakeListChatModel(responses=["a summary"]),
        trigger=SUMMARIZATION_TRIGGER,
        keep=keep,
        trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS,
    )


def _kept_tokens(keep, msgs) -> int:
    mw = _middleware(keep)
    return count_tokens_approximately(msgs[mw._determine_cutoff_index(msgs):])


# ---------------------------------------------------------------------------
# the configured setting
# ---------------------------------------------------------------------------


def test_keep_is_expressed_in_the_same_unit_as_the_trigger():
    """A token trigger with a message keep is the unit mismatch itself."""
    assert SUMMARIZATION_KEEP[0] == "tokens"


def test_keep_leaves_real_headroom_under_the_trigger():
    kept_budget = SUMMARIZATION_KEEP[1]
    assert kept_budget < TRIGGER_TOKENS / 2, (
        "keep must reclaim over half the trigger, or summarization re-fires almost immediately"
    )


def test_summarization_reclaims_most_of_the_context_on_a_file_heavy_history():
    msgs = _file_heavy_history(8)
    before = count_tokens_approximately(msgs)
    assert before > TRIGGER_TOKENS, "fixture must actually trip the trigger"
    after = _kept_tokens(SUMMARIZATION_KEEP, msgs)
    assert after <= SUMMARIZATION_KEEP[1] * 1.5, f"kept {after} against a {SUMMARIZATION_KEEP[1]} budget"
    assert after < before / 2, f"reclaimed only {before - after} of {before}"


def test_the_message_based_setting_this_replaced_reclaims_far_less():
    """Pins the actual defect, so nobody reverts to a message count."""
    msgs = _file_heavy_history(8)
    old = _kept_tokens(("messages", 20), msgs)
    new = _kept_tokens(SUMMARIZATION_KEEP, msgs)
    assert new < old, f"token keep {new} should retain less than message keep {old}"


def test_headroom_survives_several_more_large_reads():
    """The point of the fix: many reads before the next summarization, not two."""
    msgs = _file_heavy_history(8)
    after = _kept_tokens(SUMMARIZATION_KEEP, msgs)
    one_read = count_tokens_approximately([ToolMessage(content=_big_read(99), tool_call_id="x")])
    assert (TRIGGER_TOKENS - after) / one_read >= 4


# ---------------------------------------------------------------------------
# the degenerate case -- must be impossible by construction
# ---------------------------------------------------------------------------


def test_the_kept_window_can_never_exceed_the_trigger():
    """If it could, summarization would fire before every model call forever."""
    for n in (4, 8, 16, 32):
        assert _kept_tokens(SUMMARIZATION_KEEP, _file_heavy_history(n)) < TRIGGER_TOKENS


def test_a_message_keep_can_exceed_the_trigger_on_this_shape():
    """Why the old setting was unsafe and not merely suboptimal: 20 messages of
    inline file reads is more context than the trigger allows in total."""
    assert _kept_tokens(("messages", 20), _file_heavy_history(16)) > TRIGGER_TOKENS


# ---------------------------------------------------------------------------
# cutting must not corrupt the message sequence
# ---------------------------------------------------------------------------


def test_a_single_message_larger_than_the_budget_still_lands_above_it():
    """The known limit of a token budget: the cutoff search cannot cut INSIDE a
    message, and the library always keeps at least one. So one oversized tool
    result defeats the budget on its own. This is why the read tools cap
    inline content (planning: 40k chars; agent_tools.read: offloads above
    READ_INLINE_CAP_CHARS) -- the cap is what makes this budget enforceable,
    not an unrelated nicety. Documented as a test so removing a read cap fails
    here rather than silently reintroducing the thrash."""
    oversized = "x" * (SUMMARIZATION_KEEP[1] * 8)
    msgs = _file_heavy_history(2) + [
        AIMessage(content="", tool_calls=[{"name": "read_project_file", "args": {}, "id": "big"}]),
        ToolMessage(content=oversized, tool_call_id="big"),
    ]
    assert _kept_tokens(SUMMARIZATION_KEEP, msgs) > SUMMARIZATION_KEEP[1]


def test_the_preserved_window_never_starts_with_an_orphaned_tool_message():
    """A ToolMessage whose AIMessage was summarized away is an unanswered
    tool_call on the wire -- providers reject that outright."""
    for n in (4, 8, 16):
        msgs = _file_heavy_history(n)
        mw = _middleware(SUMMARIZATION_KEEP)
        kept = msgs[mw._determine_cutoff_index(msgs):]
        assert kept, "summarization must never preserve nothing"
        assert not isinstance(kept[0], ToolMessage)


def test_planning_uses_its_own_deliberately_wider_window():
    """These constants USED to be shared. They are not any more, and the split
    is the point rather than an oversight.

    Sharing them is what produced the 2026-08-31 loop: 80k trigger minus 30k
    keep is 50k of headroom, a planning read is capped at 40k chars, so three
    big reads refilled it. A build task edits a file at a time and is fine
    there; planning has to hold every file a cross-cutting question spans, and
    when it cannot it re-reads them forever (src/core/bot.js, 129 times, until
    the budget guard stopped the turn).

    So: planning must keep MORE than a build task, never less, and must go on
    importing its own constants rather than quietly falling back to the shared
    pair."""
    import agent.planning_chat as pc

    assert pc.PLANNING_SUMMARIZATION_KEEP[1] > SUMMARIZATION_KEEP[1]
    assert not hasattr(pc, "SUMMARIZATION_KEEP"), (
        "planning must not import the build-task keep again -- that is the regression"
    )
