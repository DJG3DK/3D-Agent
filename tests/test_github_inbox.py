"""The GitHub inbox: discovery from a fake API, policy decisions, approve
links, and a full poll pass against a fake store."""
import base64
import secrets
import time
from types import SimpleNamespace

import pytest

from agent import github_inbox as gi
from agent import github_settings as gs


def _config():
    return SimpleNamespace(auth_secret_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), github_token="env-token-0123456789abcdef")


class FakeGitHub:
    """Just enough of the API surface discover() touches."""
    def __init__(self, prs=(), reviews=None, alerts=(), checks=(), default_branch="main", fail=(), workflow_runs=(), code_alerts=()):
        self.prs, self.reviews_by, self.alerts, self.checks = list(prs), reviews or {}, list(alerts), list(checks)
        self.default_branch, self.fail, self.workflow_runs_list = default_branch, set(fail), list(workflow_runs)
        self.code_alerts = list(code_alerts)

    async def code_scanning_alerts(self, slug):
        if "code" in self.fail:
            raise PermissionError("Code scanning alerts: read is missing")
        if "code-404" in self.fail:
            raise LookupError("no analysis found")
        return self.code_alerts

    async def open_prs(self, slug):
        if "prs" in self.fail:
            raise PermissionError("no")
        return self.prs

    async def reviews(self, slug, number):
        return self.reviews_by.get(number, [])

    async def dependabot_alerts(self, slug):
        if "alerts" in self.fail:
            raise PermissionError("Dependabot alerts: read is missing")
        return self.alerts

    async def repo(self, slug):
        return {"default_branch": self.default_branch}

    async def check_runs(self, slug, ref):
        if "checks" in self.fail:
            raise PermissionError("Checks: read is missing")
        return self.checks

    async def workflow_runs(self, slug, branch):
        if "actions" in self.fail:
            raise PermissionError("Actions: read is missing")
        return self.workflow_runs_list


def _pr(n, login, title="bump", sha="abc123def456789"):
    return {"number": n, "title": title, "html_url": f"https://gh/pr/{n}", "user": {"login": login},
            "head": {"sha": sha, "ref": f"dep/{n}"}, "base": {"ref": "main"}}


def _proj(**policies):
    p = dict(gs.DEFAULT_PROJECT)
    p["policies"] = {**gs.DEFAULT_PROJECT["policies"], **policies}
    return p


@pytest.mark.asyncio
async def test_discover_reads_every_switched_on_source_and_one_failure_hides_nothing():
    gh = FakeGitHub(
        prs=[_pr(1, "dependabot[bot]"), _pr(2, "renovate[bot]"), _pr(3, "danny")],
        reviews={3: [{"id": 5, "state": "CHANGES_REQUESTED", "user": {"login": "reviewer"}},
                     {"id": 6, "state": "APPROVED", "user": {"login": "other"}}],
                 1: [{"id": 7, "state": "CHANGES_REQUESTED", "user": {"login": "r"}},
                     {"id": 8, "state": "APPROVED", "user": {"login": "r"}}]},   # later approval cancels it
        alerts=[{"number": 9, "state": "open", "html_url": "https://gh/alert/9",
                 "security_advisory": {"severity": "high", "summary": "RCE", "ghsa_id": "GHSA-1"},
                 "dependency": {"package": {"name": "multer"}, "manifest_path": "apps/api/package.json"},
                 "security_vulnerability": {"vulnerable_version_range": "< 2.0", "first_patched_version": {"identifier": "2.0.0"}}}],
        checks=[{"id": 11, "name": "ci", "conclusion": "failure", "head_sha": "feedfacefeed1234", "html_url": "https://gh/run/11", "output": {"title": "3 tests failed"}},
                {"id": 12, "name": "lint", "conclusion": "success", "head_sha": "feedfacefeed1234"}],
    )
    proj = _proj(dependabot_prs="propose", review_requests="propose", security_alerts="auto", ci_failures="propose")
    items = await gi.discover(gh, "proj", "o/proj", proj)
    keys = sorted(i.key for i in items)
    assert keys == ["alert:9", "ci:feedfacefeed:ci", "pr:1", "review:3"]
    by = {i.key: i for i in items}
    assert by["alert:9"].title.startswith("[HIGH] multer")
    assert "reviewer" in by["review:3"].summary
    assert by["ci:feedfacefeed:ci"].summary == "3 tests failed"

    # author filter widens to any bot, then anyone
    proj["authors"] = "bots"
    assert sorted(i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(dependabot_prs="propose") | {"authors": "bots"})) == ["pr:1", "pr:2"]
    assert len([i for i in await gi.discover(gh, "proj", "o/proj", _proj(dependabot_prs="propose") | {"authors": "anyone"}) if i.kind == "dependabot_prs"]) == 3

    # the alerts endpoint refusing (missing permission) does not hide the PRs
    gh.fail.add("alerts")
    items = await gi.discover(gh, "proj", "o/proj", proj)
    assert "pr:1" in {i.key for i in items} and "alert:9" not in {i.key for i in items}


def _item(key, kind="dependabot_prs", fp="a"):
    return gi.Item(key=key, kind=kind, repo="proj", title=key, url="u", fingerprint=fp, number=1)


def test_decide_new_items_follow_policy_and_the_auto_cap():
    proj = _proj(dependabot_prs="auto", security_alerts="propose", ci_failures="off") | {"max_open_auto": 2}
    found = [_item("pr:1"), _item("pr:2"), _item("pr:3"), _item("alert:1", "security_alerts"), _item("ci:x", "ci_failures")]
    decisions, gone = gi.decide({}, found, proj, open_auto=1)
    actions = {d.item.key: (d.action, d.item.state) for d in decisions}
    assert actions["pr:1"] == ("create", "task_created")
    assert actions["pr:2"] == ("propose", "proposed")          # cap: 2 max, 1 already open
    assert actions["pr:3"] == ("propose", "proposed")
    assert actions["alert:1"] == ("propose", "proposed")
    assert actions["ci:x"] == ("none", "seen")
    assert gone == []


def test_decide_unchanged_items_are_left_alone_and_vanished_ones_resolved():
    proj = _proj(dependabot_prs="propose")
    existing = {
        "pr:1": {"key": "pr:1", "fingerprint": "a", "state": "dismissed", "created_at": 1},
        "pr:2": {"key": "pr:2", "fingerprint": "a", "state": "proposed", "created_at": 1},
        "pr:9": {"key": "pr:9", "fingerprint": "a", "state": "proposed", "created_at": 1},
    }
    decisions, gone = gi.decide(existing, [_item("pr:1"), _item("pr:2")], proj, open_auto=0)
    assert decisions == []                      # nothing changed: no re-proposal, no spam
    assert gone == ["pr:9"]                     # merged/closed on GitHub

    # A new head commit is a new fingerprint: a dismissed PR comes back.
    decisions, _ = gi.decide(existing, [_item("pr:1", fp="b")], proj, open_auto=0)
    assert [(d.item.key, d.action) for d in decisions] == [("pr:1", "propose")]
    assert decisions[0].item.created_at == 1    # history kept

    # ...but one with a task on it keeps the task instead of stacking another.
    existing["pr:2"].update({"state": "task_created", "task_id": "t-1"})
    decisions, _ = gi.decide(existing, [_item("pr:2", fp="b")], proj, open_auto=0)
    assert decisions[0].action == "none" and decisions[0].item.task_id == "t-1"


def test_decide_reproposes_an_expired_snooze():
    proj = _proj(dependabot_prs="propose")
    existing = {"pr:1": {"key": "pr:1", "fingerprint": "a", "state": "snoozed", "snoozed_until": time.time() - 1, "created_at": 1}}
    decisions, _ = gi.decide(existing, [_item("pr:1")], proj, open_auto=0)
    assert [(d.action, d.reason) for d in decisions] == [("propose", "snooze expired")]
    existing["pr:1"]["snoozed_until"] = time.time() + 3600
    assert gi.decide(existing, [_item("pr:1")], proj, open_auto=0)[0] == []


def test_approve_links_are_signed_expiring_and_bound_to_one_item():
    # verify_approval raises reason CODES; the approve page maps them to wording.
    cfg = _config()
    tok = gi.sign_approval(cfg, "proj", "pr:1", "nonce1", "approve")
    data = gi.verify_approval(cfg, tok)
    assert (data["r"], data["k"], data["n"], data["a"]) == ("proj", "pr:1", "nonce1", "approve")

    with pytest.raises(ValueError, match="^expired$"):
        gi.verify_approval(cfg, gi.sign_approval(cfg, "proj", "pr:1", "n", "approve", ttl_s=-1))
    with pytest.raises(ValueError, match="^invalid$"):
        gi.verify_approval(_config(), tok)                       # another deployment's key
    with pytest.raises(ValueError, match="^invalid$"):
        body, mac = tok.split(".")
        gi.verify_approval(cfg, body[:-2] + "AA." + mac)          # tampered payload
    with pytest.raises(ValueError, match="^malformed$"):
        gi.verify_approval(cfg, "garbage")

    settings = gs.normalize({"public_url": "https://agent.example.com/v2"})
    assert gi.approval_url(settings, tok) == f"https://agent.example.com/v2/api/github/approve?t={tok}"
    assert gi.approval_url(gs.normalize(None), tok) is None


def test_goal_and_notification_text_name_the_gate():
    item = _item("pr:12")
    item.title, item.url, item.number, item.summary = "bump lodash", "https://gh/pr/12", 12, "dependabot: dep/12 -> main"
    goal = gi.build_goal(item, "PR #12: bump lodash\nstate: open")
    assert "pull request #12" in goal and "github_pull_request" in goal and "review gate" in goal
    assert "PR #12: bump lodash" in goal
    text = gi.proposal_text(item, "https://x/approve", "https://x/dismiss", 3.0)
    assert "Approve → https://x/approve" in text and "$3.00" in text and "Dismiss → https://x/dismiss" in text
    assert "dashboard" in gi.proposal_text(item, None, None, 3.0)


class FakeStore:
    def __init__(self):
        self.data = {}

    async def asearch(self, ns, limit=100):
        return [SimpleNamespace(key=k, value=v) for (n, k), v in self.data.items() if n == ns]

    async def aput(self, ns, key, value):
        self.data[(ns, key)] = dict(value)

    async def aget(self, ns, key):
        v = self.data.get((ns, key))
        return SimpleNamespace(key=key, value=v) if v is not None else None


@pytest.mark.asyncio
async def test_poll_project_creates_proposes_notifies_and_resolves(monkeypatch):
    cfg = _config()
    store = FakeStore()
    settings = gs.apply_patch(cfg, gs.normalize({"public_url": "https://agent.example.com/v2"}), {
        "projects": {"proj": {"policies": {"dependabot_prs": "auto", "security_alerts": "propose"}, "max_open_auto": 1, "budget_usd": 2.5}},
    })
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    monkeypatch.setattr(gi, "pr_text_for", _async_none)
    _with_checks(monkeypatch)
    created, notes = [], []

    async def create_task(repo, goal, budget, route):
        created.append((repo, goal, budget, route))
        return f"task-{len(created)}"

    async def notify(text, repo):
        notes.append(text)

    async def open_auto(repo):
        return 0

    gh = FakeGitHub(prs=[_pr(1, "dependabot[bot]", "bump a"), _pr(2, "dependabot[bot]", "bump b")],
                    alerts=[{"number": 9, "state": "open", "html_url": "u", "security_advisory": {"severity": "low", "summary": "x", "ghsa_id": "G"},
                             "dependency": {"package": {"name": "p"}}, "security_vulnerability": {}}])
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify, open_auto_count=open_auto, client=gh)
    assert summary == {"repo": "proj", "found": 3, "proposed": 2, "created": 1, "resolved": 0}
    assert len(created) == 1 and created[0][2] == 2.5 and "pull request #1" in created[0][1]
    items = await gi.list_items(store, "proj")
    assert items["pr:1"]["state"] == "task_created" and items["pr:1"]["task_id"] == "task-1"
    assert items["pr:2"]["state"] == "proposed" and "cap" in items["pr:2"]["reason"]
    assert items["alert:9"]["state"] == "proposed" and items["alert:9"]["approval_nonce"]
    approve_notes = [n for n in notes if "Approve →" in n]
    assert len(approve_notes) == 2 and "/api/github/approve?t=" in approve_notes[0]
    assert any("started automatically" in n for n in notes)

    # Second pass: nothing new, nothing re-sent; PR 2 merged -> resolved.
    notes.clear()
    gh.prs = [_pr(1, "dependabot[bot]", "bump a")]
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify, open_auto_count=open_auto, client=gh)
    assert summary["proposed"] == 0 and summary["created"] == 0 and summary["resolved"] == 1
    assert notes == [] and len(created) == 1
    assert (await gi.list_items(store, "proj"))["pr:2"]["state"] == "resolved"


@pytest.mark.asyncio
async def test_poll_project_falls_back_to_a_proposal_when_auto_start_fails(monkeypatch):
    cfg = _config()
    store = FakeStore()
    settings = gs.apply_patch(cfg, gs.normalize(None), {"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    monkeypatch.setattr(gi, "pr_text_for", _async_none)
    _with_checks(monkeypatch)
    notes = []

    async def create_task(repo, goal, budget, route):
        raise RuntimeError("classifier down")

    async def notify(text, repo):
        notes.append(text)

    async def open_auto(repo):
        return 0

    gh = FakeGitHub(prs=[_pr(1, "dependabot[bot]")])
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify, open_auto_count=open_auto, client=gh)
    assert summary["created"] == 0 and summary["proposed"] == 1
    item = (await gi.list_items(store, "proj"))["pr:1"]
    assert item["state"] == "proposed" and "auto start failed" in item["reason"]
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_poll_project_skips_without_a_token_or_a_remote(monkeypatch):
    cfg = SimpleNamespace(auth_secret_key=_config().auth_secret_key, github_token=None)
    settings = gs.apply_patch(cfg, gs.normalize(None), {"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    out = await gi.poll_project(FakeStore(), cfg, settings, "proj", create_task=None, notify=None, open_auto_count=None)
    assert out["skipped"] == "no token"


def _with_checks(monkeypatch, value: bool | None = True):
    """The poll asks the review service whether the project verifies anything
    before it auto-starts work. In a test there is no review service, so say
    what the project is: with checks unless the test is about their absence."""
    import agent.tools.review_gate as rg

    async def _has_checks(repo):
        return value

    monkeypatch.setattr(rg, "project_has_checks", _has_checks)


async def _async_none(*a, **k):
    return None


@pytest.mark.asyncio
async def test_ci_failures_fall_back_to_actions_runs_when_check_runs_are_refused():
    tip, older = "aaaaaaaaaaaa1234", "bbbbbbbbbbbb5678"
    gh = FakeGitHub(fail={"checks"}, workflow_runs=[
        {"id": 1, "name": "CI", "conclusion": "failure", "head_sha": tip, "html_url": "https://gh/run/1", "display_title": "fix: thing", "event": "push", "run_number": 40},
        {"id": 2, "name": "Deploy", "conclusion": "success", "head_sha": tip},
        {"id": 3, "name": "CI", "conclusion": "failure", "head_sha": older},   # not the tip: history, not work
        {"id": 4, "name": "npm_and_yarn in /. for sharp - Update #1", "conclusion": "failure", "head_sha": tip, "event": "dynamic"},  # Dependabot's own job
    ])
    items = await gi.discover(gh, "proj", "o/proj", _proj(ci_failures="propose"))
    assert [i.key for i in items] == [f"ci:{tip[:12]}:CI"]
    assert "fix: thing" in items[0].summary and "#40" in items[0].summary

    # Neither permission: the source is empty and the others are untouched.
    gh = FakeGitHub(fail={"checks", "actions"}, prs=[_pr(1, "dependabot[bot]")])
    items = await gi.discover(gh, "proj", "o/proj", _proj(ci_failures="propose", dependabot_prs="propose"))
    assert [i.key for i in items] == ["pr:1"]


def _code_alert(n, rule_id, sev, path, line, msg="tainted", sha="cafebabe0000", tool="CodeQL"):
    return {"number": n, "state": "open", "html_url": f"https://gh/code/{n}",
            "rule": {"id": rule_id, "security_severity_level": sev, "description": f"{rule_id} description"},
            "tool": {"name": tool},
            "most_recent_instance": {"commit_sha": sha, "message": {"text": msg},
                                     "location": {"path": path, "start_line": line}}}


@pytest.mark.asyncio
async def test_code_scanning_alerts_group_by_rule_and_scope_to_the_repo():
    gh = FakeGitHub(code_alerts=[
        _code_alert(1, "js/double-escaping", "high", "apps/a/x.ts", 10),
        _code_alert(2, "js/double-escaping", "high", "apps/b/y.ts", 20),
        _code_alert(3, "js/request-forgery", "critical", "apps/api/z.ts", 30, msg="URL from supplier"),
        {**_code_alert(4, "js/request-forgery", "critical", "old.ts", 1), "state": "fixed"},   # not open: ignored
    ])
    items = await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose"))
    assert [i.key for i in items] == ["code:js.request-forgery", "code:js.double-escaping"]   # critical first
    by = {i.key: i for i in items}
    assert by["code:js.double-escaping"].title == "[HIGH] js/double-escaping: js/double-escaping description (2 locations)"
    assert by["code:js.double-escaping"].number is None
    assert "#1 apps/a/x.ts:10" in by["code:js.double-escaping"].summary and "#2 apps/b/y.ts:20" in by["code:js.double-escaping"].summary
    assert "URL from supplier" in by["code:js.request-forgery"].summary
    assert by["code:js.request-forgery"].url == "https://github.com/o/proj/security/code-scanning?query=is%3Aopen+rule%3Ajs/request-forgery"
    assert all(i.repo == "proj" for i in items)
    assert "/" not in by["code:js.double-escaping"].key.split(":", 1)[1]   # key is one URL path segment

    # fixing one of two locations changes the fingerprint; fixing both removes the item
    before = by["code:js.double-escaping"].fingerprint
    gh.code_alerts = gh.code_alerts[1:]
    after = {i.key: i for i in await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose"))}
    assert after["code:js.double-escaping"].fingerprint != before
    gh.code_alerts = [gh.code_alerts[1]]
    assert "code:js.double-escaping" not in {i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose"))}

    # a repository that never ran code scanning (404) is quiet; a missing permission hides only this source
    gh.fail.add("code-404")
    assert await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose")) == []
    gh.fail = {"code"}
    assert [i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose", dependabot_prs="propose"))] == []
    assert await gi.discover(gh, "proj", "o/proj", _proj()) == []   # off: never fetched


def test_code_scanning_goal_scopes_the_task_to_this_repository_and_forbids_suppression():
    item = gi.code_scanning_items([_code_alert(7, "js/path-injection", "high", "apps/api/c.ts", 189)], "proj", "o/proj")[0]
    goal = gi.build_goal(item)
    assert "THIS repository" in goal and "do not look for or touch other projects" in goal
    assert "no dismissing alerts on GitHub" in goal and "#7 apps/api/c.ts:189" in goal
    assert "security/code-scanning?query=is%3Aopen+rule%3Ajs/path-injection" in goal
    assert gi.proposal_text(item, None, None, 3.0).startswith("🐙 GitHub: Code scanning alert on proj")


# ---------------------------------------------------------------------------
# Auto needs a gate with something in it (2026-09-11)
# ---------------------------------------------------------------------------

def test_auto_degrades_to_propose_when_a_project_has_no_checks():
    """A project whose review gate runs nothing mechanical has no automated
    verification behind it: a model's opinion would be the only thing between
    a GitHub alert and a diff waiting for a merge click. The operator loses
    one click and keeps the review that click is for."""
    proj = _proj(security_alerts="auto", dependabot_prs="auto")
    decisions, _ = gi.decide({}, [_item("alert:1", "security_alerts"), _item("pr:1")],
                             proj, open_auto=0, has_checks=False)
    assert [d.action for d in decisions] == ["propose", "propose"]
    assert all(d.item.state == "proposed" for d in decisions)
    assert all("no checks" in d.reason or "auto needs checks" in d.reason for d in decisions)


def test_auto_is_held_back_when_the_reviewer_cannot_be_reached():
    """Unconfirmed is not the same as none, and both fail the same way: the
    item is proposed, and the reason says which it was."""
    decisions, _ = gi.decide({}, [_item("pr:1")], _proj(dependabot_prs="auto"),
                             open_auto=0, has_checks=None)
    assert decisions[0].action == "propose"
    assert "could not confirm" in decisions[0].reason


def test_auto_still_works_for_a_project_with_checks():
    decisions, _ = gi.decide({}, [_item("pr:1")], _proj(dependabot_prs="auto"),
                             open_auto=0, has_checks=True)
    assert decisions[0].action == "create"


def test_propose_and_off_are_unaffected_by_the_checks_rule():
    """The rule is about starting work unattended, not about listing it."""
    decisions, _ = gi.decide({}, [_item("pr:1"), _item("alert:1", "security_alerts")],
                             _proj(dependabot_prs="propose", security_alerts="off"),
                             open_auto=0, has_checks=False)
    actions = {d.item.key: d.action for d in decisions}
    assert actions["pr:1"] == "propose"
    assert actions["alert:1"] == "none"


@pytest.mark.asyncio
async def test_a_full_poll_starts_nothing_on_a_project_without_checks(monkeypatch):
    """End to end through poll_project: Auto is configured, the project has
    no checks, and the pass must propose rather than create."""
    cfg = _config()
    store = FakeStore()
    settings = gs.apply_patch(cfg, gs.normalize(None),
                              {"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    monkeypatch.setattr(gi, "pr_text_for", _async_none)
    _with_checks(monkeypatch, value=False)
    created, notes = [], []

    async def create_task(repo, goal, budget, route):
        created.append(repo)
        return "task-1"

    async def notify(text, repo):
        notes.append(text)

    async def open_auto(repo):
        return 0

    gh = FakeGitHub(prs=[_pr(1, "dependabot[bot]")])
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task,
                                    notify=notify, open_auto_count=open_auto, client=gh)

    assert created == [], "auto started work on a project that verifies nothing"
    assert summary["proposed"] == 1 and summary["created"] == 0
    item = (await gi.list_items(store, "proj"))["pr:1"]
    assert item["state"] == "proposed" and "checks" in item["reason"]
    assert notes and "Approve" in notes[0]
