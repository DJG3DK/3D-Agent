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


def test_the_investigator_is_no_longer_told_to_cat():
    """Its own prompt blessed `cat` for exploration three lines before the
    appended guidance told it not to."""
    prompt = deep_agent.INVESTIGATOR_SYSTEM_PROMPT
    assert "find, grep, ls, cat, git log" not in prompt
    assert "`read` rather than `cat`" in prompt
