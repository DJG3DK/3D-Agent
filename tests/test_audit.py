"""The audit log: what it records, what it refuses to break, and who reads it.

The premise is in agent/audit.py -- Telegram is a notification channel, not a
record. These hold the three properties that make the log worth trusting:
every action in the registry is actually recorded somewhere, a failing write
never breaks the thing being audited, and the page is admin-only because it
names accounts.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from agent import audit

REPO = pathlib.Path(__file__).resolve().parent.parent


class FakeStore:
    """The three methods audit.py uses, plus a switch for making them fail."""

    def __init__(self, fail: set[str] | None = None):
        self.rows: dict[str, dict] = {}
        self.fail = fail or set()

    async def aput(self, ns, key, value):
        if "put" in self.fail:
            raise RuntimeError("database is down")
        self.rows[key] = dict(value)

    async def asearch(self, ns, limit=100):
        if "search" in self.fail:
            raise RuntimeError("database is down")
        items = [type("Item", (), {"key": k, "value": v})() for k, v in self.rows.items()]
        return items[:limit]

    async def adelete(self, ns, key):
        self.rows.pop(key, None)


def test_a_record_carries_who_what_and_when():
    store = FakeStore()
    entry = asyncio.run(audit.record(store, actor="danny@example.com", action="project.onboard",
                                     target="3DSteals", detail="/home/3DSteals"))
    assert entry["actor"] == "danny@example.com"
    assert entry["action"] == "project.onboard"
    assert entry["target"] == "3DSteals"
    assert entry["ts"] > 0
    assert len(store.rows) == 1


def test_an_unknown_action_is_refused_rather_than_stored():
    """A typo'd action has no label in the UI and nothing searches for it.
    Better to lose it loudly here than to store a row nobody will find."""
    store = FakeStore()
    assert asyncio.run(audit.record(store, actor="a@b.c", action="settings.auto_aprove")) is None
    assert store.rows == {}


def test_a_failing_store_never_breaks_the_action_being_audited():
    """An approval that fails because the log write failed would make the log
    a new way to break the system. The trade is stated in audit.py."""
    store = FakeStore(fail={"put"})
    assert asyncio.run(audit.record(store, actor="a@b.c", action="command.approve")) is None


def test_no_store_at_all_is_not_an_error():
    assert asyncio.run(audit.record(None, actor="a@b.c", action="command.approve")) is None
    assert asyncio.run(audit.recent(None)) == []


def test_recent_is_newest_first_and_labelled():
    store = FakeStore()
    for i, action in enumerate(["project.onboard", "command.approve", "deploy_key.generate"]):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action=action))
    rows = asyncio.run(audit.recent(store, limit=10))
    assert [r["action"] for r in rows] == ["deploy_key.generate", "command.approve", "project.onboard"]
    assert rows[0]["label"] == audit.ACTIONS["deploy_key.generate"]


def test_recent_survives_a_malformed_row():
    """One bad row must not blank the page."""
    store = FakeStore()
    asyncio.run(audit.record(store, actor="a@b.c", action="command.approve"))
    store.rows["junk"] = {"not": "an entry"}
    store.rows["alsojunk"] = None
    rows = asyncio.run(audit.recent(store))
    assert len(rows) == 1


def test_recent_survives_an_unreadable_store():
    store = FakeStore(fail={"search"})
    assert asyncio.run(audit.recent(store)) == []


def test_keys_sort_in_time_order():
    """The store has no ORDER BY for us to use, so the key has to carry it."""
    keys = [audit._key(t) for t in (1.0, 2.5, 1000.0, 1789000000.123)]
    assert keys == sorted(keys)


def test_the_log_is_trimmed_rather_than_growing_without_bound(monkeypatch):
    monkeypatch.setattr(audit, "MAX_RECORDS", 5)
    monkeypatch.setattr(audit, "_TRIM_EVERY", 3)
    monkeypatch.setattr(audit, "_writes_since_trim", 0)
    store = FakeStore()
    for i in range(24):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action="command.approve", detail=str(i)))
    assert len(store.rows) <= audit.MAX_RECORDS * 2
    # and what survives is the newest
    kept = sorted((r["detail"] for r in store.rows.values()), key=int)
    assert kept[-1] == "23"


@pytest.mark.parametrize("action", sorted(audit.ACTIONS))
def test_every_registered_action_is_recorded_somewhere(action):
    """An action in the table that no call site ever writes is a promise the
    page cannot keep: the operator reads the log, sees no entry, and concludes
    it did not happen."""
    sources = "\n".join(p.read_text() for p in (REPO / "agent").rglob("*.py")
                        if p.name != "audit.py")
    assert f'"{action}"' in sources, f"{action} is in ACTIONS and nothing records it"


def test_every_recorded_action_is_in_the_registry():
    """The other direction: a call site using a name the registry does not
    know is dropped at write time (see the unknown-action test), so this is
    the test that catches it before an operator loses a record."""
    used = set()
    for path in (REPO / "agent").rglob("*.py"):
        if path.name == "audit.py":
            continue
        for m in re.finditer(r'action="([a-z_]+\.[a-z_]+)"', path.read_text()):
            used.add(m.group(1))
    unknown = used - set(audit.ACTIONS)
    assert not unknown, f"recorded but not in ACTIONS (they would be dropped): {sorted(unknown)}"
