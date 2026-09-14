"""What the agent is told about the two filesystems, and about the container.

Both gaps were found by watching a live build task on 2026-09-12 (279c29fd):

* it reached for /memories/AGENTS.md through bash twice. The prompt stated one
  direction of the split -- the built-in read_file cannot see the repo -- but
  never the reverse, that bash cannot see the agent's own memory.
* it spent several containers discovering that bash keeps no state between
  calls: a persist-probe written across five directories, then a
  `nohup curl ... &` whose process dies with the container, then a call
  looking for the file it downloaded. The tool said "runs inside an isolated
  sandbox container" and never that each call gets a NEW one.

These assert on meaning, not wording -- a rewrite is free, a silent deletion
is what this is here to catch.
"""

from __future__ import annotations

import pytest

from agent import deep_agent
from agent.tools import agent_tools


@pytest.fixture
def bash_doc(tmp_path):
    tools, _ = agent_tools.make_agent_tools(str(tmp_path))
    return {t.name: t for t in tools}["bash"].description


def test_bash_says_every_call_gets_a_fresh_container(bash_doc):
    lowered = bash_doc.lower()
    assert "fresh container" in lowered
    assert "/workspace" in bash_doc
    # the three concrete consequences the live run got wrong
    assert "/tmp" in bash_doc, "the thing it tried to persist"
    assert "nohup" in lowered, "the background process it expected to survive"
    assert "one command" in lowered or "one call" in lowered


def test_bash_says_it_cannot_see_the_agents_own_memory(bash_doc):
    for path in ("/memories", "/skills", "/org-memory"):
        assert path in bash_doc
    assert "read_file" in bash_doc and "write_file" in bash_doc


def test_the_shared_guidance_states_both_directions():
    guidance = deep_agent._FILESYSTEM_GUIDANCE
    # direction one: the built-in tools cannot see the repo
    assert "NEVER the actual repo code" in guidance
    # direction two: bash cannot see the agent's own filesystem
    assert "CANNOT see /memories/" in guidance
    assert "discarded" in guidance, "a shell write there reports success and is thrown away"


def test_every_subagent_that_has_bash_gets_the_guidance():
    """It is appended per prompt rather than shared by construction, so a new
    subagent can be added without it. Catch that here."""
    for prompt in (deep_agent.INVESTIGATOR_SYSTEM_PROMPT,
                   deep_agent.GENERAL_PURPOSE_SUBAGENT["system_prompt"] + "\n\n"
                   + deep_agent._FILESYSTEM_GUIDANCE):
        assert "two separate filesystems" in prompt


def test_the_investigator_leads_with_read_not_bash():
    """Its prompt used to open with "You DO have `bash` (needed for real
    find/grep-style exploration)" and hang the read advice off the end as a
    subordinate clause. For a subagent whose whole job is exploration, the
    first half was the load-bearing sentence -- and measured on task 3ee0d030
    its read use fell from 46 calls to 14 across one run while bash went 91 to
    132. `read` is named first now, and bash is scoped to what only bash can
    do rather than blessed for exploration generally."""
    prompt = deep_agent.INVESTIGATOR_SYSTEM_PROMPT
    assert "find, grep, ls, cat, git log" not in prompt
    assert "You DO have `bash` (needed for real find/grep-style exploration" not in prompt
    read_at, bash_at = prompt.index("`read`"), prompt.index("`bash`")
    assert read_at < bash_at, "read has to be introduced before bash"


def test_both_prompts_say_several_reads_can_go_in_one_turn():
    """The fact the model had no way to know. It emits parallel tool calls
    already; nothing told it that N reads in one turn beat one `cat a b c`."""
    for prompt in (deep_agent.INVESTIGATOR_SYSTEM_PROMPT, deep_agent._FILESYSTEM_GUIDANCE):
        assert "SAME TURN" in prompt
        assert "offset/limit" in prompt


def test_the_prompts_say_bash_is_the_only_way_to_search():
    """Scoping bash must not read as "avoid bash". glob/grep are hidden
    because they cannot see the repo, so bash searching is correct and the
    prompt has to say so, or the nudge starts costing real work."""
    for prompt in (deep_agent.INVESTIGATOR_SYSTEM_PROMPT, deep_agent._FILESYSTEM_GUIDANCE):
        assert "only way to search" in prompt or "only bash can do" in prompt
