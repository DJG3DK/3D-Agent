"""Regression test for a key-consistency bug: app code (seeding,
system-prompt reads) used full agent-visible paths as raw store keys
('/memories/AGENTS.md'), while the agent's own file tools go through the
CompositeBackend, whose route stripping stores the same file at '/AGENTS.md'.
Each half was self-consistent, so everything looked fine from either side
alone -- but agent-written memory updates were invisible to every future
task's prompt read, and no seeded skill could be opened by the agent at all.

These tests pin the contract that fixed it: whatever seed_memory/seed_skill
write must be readable through the composite backend at the agent-visible
path, and whatever the agent writes through the composite must be readable
by the (stripped-key) prompt-read path -- using a real in-memory store, both
directions, not just one half at a time.
"""

from langgraph.store.memory import InMemoryStore

from deepagents.backends import StoreBackend

from agent.deep_agent import (
    MEMORY_PATH,
    ORG_MEMORY_PATH,
    org_namespace,
    build_memory_backend,
    load_skills_summary,
    project_namespace,
    read_memory_or_empty,
    route_local_path,
    seed_memory,
    seed_org_memory,
    seed_skill,
)
from deepagents.backends.utils import file_data_to_string


def test_route_local_path_strips_to_leading_slash():
    assert route_local_path("/memories/", "/memories/AGENTS.md") == "/AGENTS.md"
    assert route_local_path("/skills/", "/skills/foo/SKILL.md") == "/foo/SKILL.md"


async def test_seeded_memory_is_readable_through_the_agents_composite_backend():
    store = InMemoryStore()
    await seed_memory("test-repo", store, "seeded project memory")
    composite = build_memory_backend("test-repo", store)
    result = await composite.aread(MEMORY_PATH)
    assert result.error is None, f"agent tools can't see the seed: {result.error}"
    assert "seeded project memory" in file_data_to_string(result.file_data)


async def test_agent_written_memory_is_readable_by_the_prompt_read_path():
    """The direction that was actually broken live: the agent extends its
    memory via its file tools (composite), and the NEXT task's prompt read
    (bare backend, stripped key) must see that update."""
    store = InMemoryStore()
    composite = build_memory_backend("test-repo", store)
    await composite.awrite(MEMORY_PATH, "something the agent learned")

    prompt_backend = StoreBackend(namespace=project_namespace("test-repo"), store=store)
    content = await read_memory_or_empty(prompt_backend, route_local_path("/memories/", MEMORY_PATH))
    assert "something the agent learned" in content


async def test_seed_does_not_clobber_agent_written_memory():
    store = InMemoryStore()
    composite = build_memory_backend("test-repo", store)
    await composite.awrite(MEMORY_PATH, "agent knowledge")
    await seed_memory("test-repo", store, "the seed")  # idempotent-skip must see the agent's file
    result = await composite.aread(MEMORY_PATH)
    assert "agent knowledge" in file_data_to_string(result.file_data)
    assert "the seed" not in file_data_to_string(result.file_data)


async def test_seeded_org_memory_is_readable_through_the_composite():
    store = InMemoryStore()
    await seed_org_memory(store, "org-wide policy")
    composite = build_memory_backend("test-repo", store)  # any repo -- org route is shared
    result = await composite.aread(ORG_MEMORY_PATH)
    assert result.error is None
    assert "org-wide policy" in file_data_to_string(result.file_data)


async def test_seeded_skill_is_readable_at_the_path_the_summary_advertises():
    """load_skills_summary tells the model to read /skills/<name>/SKILL.md --
    the exact live failure was that this advertised path errored for every
    seeded skill. Seed one, then follow the summary's own instruction
    through the agent's composite backend."""
    store = InMemoryStore()
    await seed_skill("test-repo", store, "example-skill", "how the subsystem works", "FULL SKILL CONTENT")

    summary = await load_skills_summary("test-repo", store)
    assert "example-skill" in summary
    advertised_path = "/skills/example-skill/SKILL.md"
    assert advertised_path in summary

    composite = build_memory_backend("test-repo", store)
    result = await composite.aread(advertised_path)
    assert result.error is None, f"the summary advertises a path the agent can't read: {result.error}"
    assert "FULL SKILL CONTENT" in file_data_to_string(result.file_data)
