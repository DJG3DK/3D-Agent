"""content_text + the live-view translators against Kimi's block-list format.

Kimi K3 via OpenRouter returns AIMessage.content as a list of typed blocks
(text + tool_call), not a string. Both dashboard translators rendered
`str(msg.content)`, so the operator saw raw dict repr -- and for a turn whose
only block was the tool_call, the repr was ALL they saw. In a real planning
session (2026-08-27, 76 messages) that was 32 messages of block debris and 21
"no output" turns, read as tool failures when every tool call had in fact
succeeded.
"""

from langchain_core.messages import AIMessage, ToolMessage

from agent.message_text import content_text
from agent.planning_chat import _translate_message as translate_planning
from agent.nodes.work import _translate_message as translate_work

KIMI_BLOCKS = [
    {"type": "text", "text": "Let me read the strategy file.", "index": 0},
    {"type": "tool_call", "id": "chatcmpl-tool-9478c27bf2c5cc9f",
     "name": "read_project_file", "args": {"repo": "my-service", "path": "x.js"}},
]
TOOL_CALLS = [{"name": "read_project_file", "args": {"repo": "my-service", "path": "x.js"},
               "id": "chatcmpl-tool-9478c27bf2c5cc9f"}]


# ---------------------------------------------------------------------------
# content_text
# ---------------------------------------------------------------------------


def test_plain_string_passes_through():
    assert content_text("hello") == "hello"


def test_extracts_text_blocks_and_drops_tool_call_blocks():
    assert content_text(KIMI_BLOCKS) == "Let me read the strategy file."


def test_a_tool_call_only_list_yields_empty_not_dict_repr():
    assert content_text([KIMI_BLOCKS[1]]) == ""


def test_handles_none_and_bare_strings_in_lists():
    assert content_text(None) == ""
    assert content_text(["a", {"type": "text", "text": "b"}]) == "ab"


# ---------------------------------------------------------------------------
# the translators, on the exact message shape from the live session
# ---------------------------------------------------------------------------


def _kimi_ai_message():
    return AIMessage(content=KIMI_BLOCKS, tool_calls=TOOL_CALLS)


def _tool_call_only_message():
    return AIMessage(content=[KIMI_BLOCKS[1]], tool_calls=TOOL_CALLS)


def test_planning_translator_shows_prose_not_dict_repr():
    entry = translate_planning(_kimi_ai_message())
    assert "{'type':" not in entry["detail"]
    assert "Let me read the strategy file." in entry["detail"]
    assert entry["summary"].startswith("calling: read_project_file")


def test_planning_translator_tool_call_only_turn_shows_the_call_not_debris():
    """The '21 no-output calls' shape: no text block at all."""
    entry = translate_planning(_tool_call_only_message())
    assert "{'type':" not in entry["detail"]
    assert entry["detail"].startswith("calling: read_project_file")


def test_work_translator_shows_prose_not_dict_repr():
    entry = translate_work("t1", "coordinator", _kimi_ai_message())
    assert "{'type':" not in entry["detail"]
    assert "Let me read the strategy file." in entry["detail"]


def test_work_translator_tool_call_only_turn_shows_the_call_not_debris():
    entry = translate_work("t1", "coordinator", _tool_call_only_message())
    assert "{'type':" not in entry["detail"]
    assert entry["detail"].startswith("calling: read_project_file")


def test_a_text_only_turn_with_list_content_is_summarized_from_its_text():
    msg = AIMessage(content=[{"type": "text", "text": "Here is the plan overview."}])
    entry = translate_planning(msg)
    assert entry["summary"] == "Here is the plan overview."


def test_an_all_internal_turn_is_skipped_not_rendered_as_debris():
    """No text, no tool_calls -- nothing an operator can act on."""
    msg = AIMessage(content=[{"type": "tool_call", "id": "x", "name": "y", "args": {}}])
    assert translate_planning(msg) is None
    assert translate_work("t1", "coordinator", msg) is None


def test_tool_results_with_block_content_render_their_text():
    msg = ToolMessage(content=[{"type": "text", "text": "file contents here"}], tool_call_id="c1")
    assert translate_planning(msg)["summary"] == "tool result: file contents here"
    assert translate_work("t1", "coordinator", msg)["summary"] == "tool result: file contents here"
