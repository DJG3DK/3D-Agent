"""Invariants for a task GitHub asked for, rather than a person.

Auto inbox + per-user auto-approve + merge review off is the combination that
turns this from an assistant into an unattended merge bot. The README promises
Auto "keeps the operator's final merge approval"; a sentence in a README is not
an invariant, so these tests are.

Both properties are decided in server._github_create_task, which reads NO user
preference: an admin who turned auto mode on for their own typed work must not
have that apply to work nobody typed.
"""
import asyncio
import inspect

import agent.server as srv


def _capture_start_task(monkeypatch):
    """Replace _start_task and return the kwargs it was called with."""
    seen: dict = {}

    async def fake_start_task(goal, repo, budget_usd, route, **kwargs):
        seen.update({"goal": goal, "repo": repo, "budget_usd": budget_usd, "route": route, **kwargs})
        return {"task_id": "task-1"}

    monkeypatch.setattr(srv, "_start_task", fake_start_task)
    return seen


def test_an_inbox_task_never_inherits_auto_approve(monkeypatch):
    seen = _capture_start_task(monkeypatch)

    task_id = asyncio.run(srv._github_create_task("proj", "fix the alert", 3.0, "auto"))

    assert task_id == "task-1"
    assert seen["auto_approve_commands"] is False, "a task nobody typed must still prompt for gated actions"
    assert seen["require_merge_review"] is True, "an inbox task must always keep the operator's merge approval"
    assert seen["origin"] == "github"


def test_the_invariant_does_not_read_any_user_preference():
    """A regression here would look like a one-word change ('admin.get(...)'),
    so pin the shape too: the function must not consult the user table."""
    source = inspect.getsource(srv._github_create_task)
    assert "auto_approve_commands=False" in source
    assert "require_merge_review=True" in source
    assert "get_user_by_email" not in source, "inbox tasks must not read a user's toggles"
    body = source.split('"""')[2]
    assert "admin" not in body, "inbox tasks must not consult the admin account"


def test_every_inbox_path_goes_through_the_same_creator():
    """Poller and dashboard-approve both create tasks; neither may build its
    own call to _start_task and skip the invariant."""
    from agent import github_inbox

    assert "create_task(" in inspect.getsource(github_inbox.create_task_for_item)
    act = inspect.getsource(srv._github_act)
    assert "create_task_for_item" in act and "_github_create_task" in act
    poll = inspect.getsource(srv._github_poll_once)
    assert "_github_create_task" in poll
