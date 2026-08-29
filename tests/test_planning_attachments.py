"""Planning chat's mid-conversation file uploads (2026-08-23): parity with
the build-task chat, which has always let an operator attach
images/PDFs/CSVs via /api/uploads. Reuses that same generic endpoint and the
same _attachments_note text -- the only new wiring is server.py threading
`attachments` from the request through to the text handed to
run_planning_turn.

Mirrors create_task's own split: classification (difficulty + category/
title) runs on the clean, operator-typed text; the attachments note is
appended only to the message actually sent to the model.
"""

from langgraph.store.memory import InMemoryStore

import agent.server as srv


class _FakeTracker:
    total_cost = 0.42


async def _fake_build_planning_agent(*args, **kwargs):
    return "fake-agent", {"markdown": None}, _FakeTracker()


def _seed_session(store, repo, session_id):
    return store.aput(("planning", repo), session_id, {
        "session_id": session_id, "repo": repo, "created_at": 0.0,
        "updated_at": 0.0, "title": None, "plan_markdown": None,
        "cost_usd": 0.0, "archived": False, "category": None,
    })


async def test_attachments_note_reaches_the_model_but_not_classification(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "checkpointer", None, raising=False)
    repo, session_id = "test-repo", "sess-1"
    await _seed_session(store, repo, session_id)

    seen_difficulty_text = []
    seen_turn_text = []

    async def fake_difficulty(text, config):
        seen_difficulty_text.append(text)
        return "EASY"

    async def fake_run_turn(agent, plan_ref, thread_config, text, publish, tracker=None):
        seen_turn_text.append(text)
        return "# plan"

    async def fake_classify_task(text, config):
        from agent.classify import TaskClassification
        return TaskClassification(category="feature", needs_tests=False)

    monkeypatch.setattr(srv, "classify_planning_difficulty", fake_difficulty)
    monkeypatch.setattr(srv, "build_planning_agent", _fake_build_planning_agent)
    monkeypatch.setattr(srv, "run_planning_turn", fake_run_turn)
    monkeypatch.setattr(srv, "classify_task", fake_classify_task)

    attachments = [{"path": ".uploads/x/shot.png", "kind": "image", "bytes": 10}]
    await srv._run_planning_turn_bg(session_id, repo, "what do you think of this screenshot?", attachments)

    assert seen_difficulty_text == ["what do you think of this screenshot?"]
    assert seen_turn_text[0].startswith("what do you think of this screenshot?")
    assert "ATTACHED FILES" in seen_turn_text[0]
    assert "describe_image" in seen_turn_text[0]

    meta = (await store.aget(("planning", repo), session_id)).value
    # The sidebar title is the clean text, not the model-facing note.
    assert meta["title"] == "what do you think of this screenshot?"


async def test_no_attachments_leaves_the_turn_text_untouched(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "checkpointer", None, raising=False)
    repo, session_id = "test-repo", "sess-2"
    await _seed_session(store, repo, session_id)

    seen_turn_text = []

    async def fake_difficulty(text, config):
        return "EASY"

    async def fake_run_turn(agent, plan_ref, thread_config, text, publish, tracker=None):
        seen_turn_text.append(text)
        return None

    async def fake_classify_task(text, config):
        from agent.classify import TaskClassification
        return TaskClassification(category="other", needs_tests=False)

    monkeypatch.setattr(srv, "classify_planning_difficulty", fake_difficulty)
    monkeypatch.setattr(srv, "build_planning_agent", _fake_build_planning_agent)
    monkeypatch.setattr(srv, "run_planning_turn", fake_run_turn)
    monkeypatch.setattr(srv, "classify_task", fake_classify_task)

    await srv._run_planning_turn_bg(session_id, repo, "just chatting, no files", None)

    assert seen_turn_text == ["just chatting, no files"]
