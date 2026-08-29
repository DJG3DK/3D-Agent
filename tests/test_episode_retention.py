"""Episode retention must never be able to delete unconsolidated history.

Episodes were previously never pruned — one row per task, forever. Pruning is
safe only because of two rules, and both are load-bearing enough to test.
"""
from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from agent.consolidation import EPISODE_RETENTION, _prune_consolidated_episodes
from agent.deep_agent import EPISODES_ROUTE, episodes_namespace


async def _seed(store, repo, n):
    ns = episodes_namespace(repo)(None)
    keys = []
    for i in range(n):
        key = f"{EPISODES_ROUTE}2026-01-{i // 30 + 1:02d}T{i % 24:02d}:00:00Z-{i:05d}.json"
        await store.aput(ns, key, {"content": "{}"})
        keys.append(key)
    return sorted(keys)


@pytest.mark.asyncio
async def test_no_pruning_below_the_retention_floor():
    store = InMemoryStore()
    keys = await _seed(store, "r", EPISODE_RETENTION - 5)
    pruned = await _prune_consolidated_episodes(store, "r", keys[-1][len(EPISODES_ROUTE):])
    assert pruned == 0
    assert len(await store.asearch(episodes_namespace("r")(None), limit=1000)) == EPISODE_RETENTION - 5


@pytest.mark.asyncio
async def test_prunes_only_down_to_the_retention_window():
    store = InMemoryStore()
    keys = await _seed(store, "r", EPISODE_RETENTION + 40)
    pruned = await _prune_consolidated_episodes(store, "r", keys[-1][len(EPISODES_ROUTE):])
    assert pruned == 40
    left = sorted(i.key for i in await store.asearch(episodes_namespace("r")(None), limit=1000))
    assert len(left) == EPISODE_RETENTION
    assert left == keys[40:], "the SURVIVORS must be the newest ones"


@pytest.mark.asyncio
async def test_never_deletes_past_the_consolidation_marker():
    """The marker is the high-water mark of what memory has absorbed. Anything
    above it has NOT been consolidated, so deleting it would lose history that
    was never distilled into /memories/AGENTS.md."""
    store = InMemoryStore()
    keys = await _seed(store, "r", EPISODE_RETENTION + 40)
    # marker sits well below the newest — simulating a run that only got partway
    marker = keys[10][len(EPISODES_ROUTE):]
    pruned = await _prune_consolidated_episodes(store, "r", marker)
    left = sorted(i.key for i in await store.asearch(episodes_namespace("r")(None), limit=1000))
    assert pruned == 11, "only the 11 episodes at/below the marker are eligible"
    assert all(k in left for k in keys[11:]), "unconsolidated episodes must survive"
