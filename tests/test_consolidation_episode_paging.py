"""audit H-21: consolidation must review the OLDEST unconsolidated episodes and
advance the marker only over what it actually reviewed -- otherwise episodes
older than the newest MAX_EPISODES_PER_RUN are skipped and later pruned unread."""
from types import SimpleNamespace

import pytest

import agent.consolidation as c
from agent.deep_agent import EPISODES_ROUTE


class _FakeDescStore:
    """Mimics AsyncPostgresStore.asearch: returns items ordered updated_at DESC
    (newest first) with limit/offset paging. Episode keys are timestamp-ordered,
    so lexical order stands in for updated_at here."""
    def __init__(self, keys):
        # store newest-first to match DESC
        self._desc = sorted(keys, reverse=True)

    async def asearch(self, ns, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None):
        window = self._desc[offset:offset + limit]
        return [SimpleNamespace(key=k) for k in window]


@pytest.mark.asyncio
async def test_oldest_pending_episodes_are_returned_first_not_the_newest():
    # 70 pending episodes, MAX_EPISODES_PER_RUN is 50 -> the first run must take
    # the 50 OLDEST (t00..t49), not the 50 newest.
    keys = [f"{EPISODES_ROUTE}t{i:02d}" for i in range(70)]
    store = _FakeDescStore(keys)

    paths = await c._list_recent_episode_paths(store, "repo", since=None)
    assert len(paths) == c.MAX_EPISODES_PER_RUN
    assert paths[0] == f"{EPISODES_ROUTE}t00", "must start at the oldest"
    assert paths[-1] == f"{EPISODES_ROUTE}t49", "must not skip the oldest for the newest"


@pytest.mark.asyncio
async def test_next_run_resumes_after_the_marker_with_no_gap():
    keys = [f"{EPISODES_ROUTE}t{i:02d}" for i in range(70)]
    store = _FakeDescStore(keys)

    first = await c._list_recent_episode_paths(store, "repo", since=None)
    marker = first[-1][len(EPISODES_ROUTE):]  # "t49"
    second = await c._list_recent_episode_paths(store, "repo", since=marker)

    # the remaining 20 (t50..t69), none skipped, none re-reviewed
    assert second[0] == f"{EPISODES_ROUTE}t50"
    assert second[-1] == f"{EPISODES_ROUTE}t69"
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 70
