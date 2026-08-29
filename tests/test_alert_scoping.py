"""H1: Telegram alerts must not leak activity across projects.

The fan-out selected recipients on "has a bot token configured" alone, while
the message body carries the repo name, an excerpt of the goal, and up to 1500
characters of escalation or exception detail. A user restricted to one repo
therefore received a live feed of every project on the deployment.

These test the filter predicate directly and the fan-out end to end with a
stubbed sender, since that is where a recipient is chosen.
"""

import pytest

from agent import notify


@pytest.mark.parametrize("role,allowed,repo,expected", [
    # an admin hears everything, including infrastructure alerts
    ("admin", None, "shop-api", True),
    ("admin", None, None, True),
    # a scoped user hears only their own repos
    ("user", ["shop-api"], "shop-api", True),
    ("user", ["shop-api"], "other-api", False),
    ("user", [], "shop-api", False),
    # unrestricted non-admins (allowed_repos NULL) keep the historical meaning
    ("user", None, "anything", True),
    # infrastructure alerts name no project: admins only, never token holders
    ("user", ["shop-api"], None, False),
    ("user", None, None, False),
])
def test_who_may_hear_about_a_repo(role, allowed, repo, expected):
    assert notify._may_hear_about(role, allowed, repo) is expected


async def test_fan_out_skips_recipients_outside_the_repo(monkeypatch):
    targets = [
        ("tok-admin", "1", "admin", None),
        ("tok-owner", "2", "user", ["shop-api"]),
        ("tok-other", "3", "user", ["unrelated"]),
    ]

    async def fake_targets(pool):
        return targets

    delivered = []

    async def fake_send(token, chat_id, text):
        delivered.append(token)
        return True

    import agent.auth as auth
    monkeypatch.setattr(auth, "get_telegram_targets", fake_targets)
    monkeypatch.setattr(notify, "send_telegram", fake_send)

    sent = await notify.notify_operators(object(), "task failed on shop-api", repo="shop-api")

    assert sent == 2
    assert delivered == ["tok-admin", "tok-owner"]
    assert "tok-other" not in delivered, "a user scoped to another repo received the alert"


async def test_infrastructure_alerts_go_to_admins_only(monkeypatch):
    """A service restart names no project, so it cannot be repo-filtered --
    send it to admins rather than to everyone holding a token."""
    async def fake_targets(pool):
        return [("tok-admin", "1", "admin", None), ("tok-user", "2", "user", ["shop-api"])]

    delivered = []

    async def fake_send(token, chat_id, text):
        delivered.append(token)
        return True

    import agent.auth as auth
    monkeypatch.setattr(auth, "get_telegram_targets", fake_targets)
    monkeypatch.setattr(notify, "send_telegram", fake_send)

    await notify.notify_operators(object(), "agent backend restarted")
    assert delivered == ["tok-admin"]
