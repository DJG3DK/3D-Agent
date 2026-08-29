"""DELETE /api/planning/sessions/{id} removes a conversation outright.

Archiving keeps a session reachable, which is right for one you might come
back to. Delete is for the ones you would not -- a conversation abandoned
part-way, or work done another way. Those now surface under "In progress"
(sessions are categorised only once they have a saved plan), so there needs
to be a way to remove them rather than only to archive them.

Mirrors DELETE /api/tasks/{id}, including refusing while a turn is in flight.
"""

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User

_USER = User(id=1, email="op@example.com", role="user", allowed_repos=["shop"],
             totp_enabled=True, must_change_password=False,
             auto_approve_commands=False, require_merge_review=True)


class _Store:
    def __init__(self):
        self.deleted = []
        self.records = {("planning", "shop"): {"s1": {"session_id": "s1", "repo": "shop"}}}

    async def aget(self, ns, key):
        rec = self.records.get(tuple(ns), {}).get(key)
        return type("I", (), {"key": key, "value": rec})() if rec else None

    async def adelete(self, ns, key):
        self.deleted.append((tuple(ns), key))
        self.records.get(tuple(ns), {}).pop(key, None)


class _Checkpointer:
    def __init__(self):
        self.threads = []

    async def adelete_thread(self, thread_id):
        self.threads.append(thread_id)


@pytest.fixture
def wired(monkeypatch):
    store, ckpt = _Store(), _Checkpointer()
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "checkpointer", ckpt, raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _USER)

    async def find(session_id):
        if session_id == "s1":
            return "shop", {"session_id": "s1", "repo": "shop"}
        return None, None

    monkeypatch.setattr(srv, "_find_planning_meta", find)
    srv._running_planning_turns.pop("s1", None)
    srv._live_planning_log.pop("s1", None)
    srv._planning_subscribers.pop("s1", None)
    return store, ckpt


def test_delete_removes_the_record_and_the_conversation(wired):
    store, ckpt = wired
    srv._live_planning_log["s1"] = [{"summary": "x"}]

    res = TestClient(srv.app).delete("/api/planning/sessions/s1")

    assert res.status_code == 200, res.text
    assert (("planning", "shop"), "s1") in store.deleted
    # the messages live under a namespaced thread id, not the bare session id
    assert "planning:s1" in ckpt.threads, (
        "the checkpointer thread was not deleted; every message would be orphaned")
    assert "s1" not in srv._live_planning_log, "the in-process log mirror survived"


def test_delete_is_refused_while_a_turn_is_running(wired):
    store, _ = wired
    srv._running_planning_turns["s1"] = object()
    try:
        res = TestClient(srv.app).delete("/api/planning/sessions/s1")
        assert res.status_code == 409
        assert store.deleted == [], "a running session was deleted anyway"
    finally:
        srv._running_planning_turns.pop("s1", None)


def test_a_refused_delete_does_not_strand_the_run_slot(wired):
    """The claim must be released when the handler rejects, or the session
    could never be used again."""
    res = TestClient(srv.app).delete("/api/planning/sessions/nope")
    assert res.status_code == 404
    assert "nope" not in srv._running_planning_turns


def test_unknown_session_is_404(wired):
    store, _ = wired
    assert TestClient(srv.app).delete("/api/planning/sessions/nope").status_code == 404
    assert store.deleted == []


def test_repo_access_is_enforced(wired, monkeypatch):
    """A user scoped to another repo must not delete this session."""
    outsider = User(id=2, email="x@example.com", role="user", allowed_repos=["other"],
                    totp_enabled=True, must_change_password=False,
                    auto_approve_commands=False, require_merge_review=True)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: outsider)
    store, _ = wired
    res = TestClient(srv.app).delete("/api/planning/sessions/s1")
    assert res.status_code in (403, 404)
    assert store.deleted == [], "a session outside the user's repos was deleted"
