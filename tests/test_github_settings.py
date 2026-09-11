"""Settings -> GitHub: tokens encrypted at rest and never echoed, policies
validated, a project's token resolved with the env fallback."""
import base64
import secrets
from types import SimpleNamespace

import pytest

from agent import github_settings as gs


def _config(github_token=None):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    return SimpleNamespace(auth_secret_key=key, github_token=github_token)


def test_tokens_round_trip_and_the_public_view_never_carries_them():
    cfg = _config()
    settings = gs.apply_patch(cfg, gs.normalize(None), {"add_tokens": {"main": "github_pat_" + "x" * 40}})
    entry = settings["tokens"]["main"]
    assert entry["enc"] != "github_pat_" + "x" * 40
    assert gs.decrypt_token(cfg, entry["enc"]) == "github_pat_" + "x" * 40
    assert entry["hint"] == "…xxxx"

    view = gs.public_view(settings)
    assert "enc" not in view["tokens"]["main"]
    assert view["tokens"]["main"]["hint"] == "…xxxx"
    assert "github_pat_" not in str(view)


def test_a_project_resolves_its_named_token_and_falls_back_to_env():
    cfg = _config(github_token="env-token-0123456789abcdef")
    settings = gs.apply_patch(cfg, gs.normalize(None), {
        "add_tokens": {"main": "github_pat_" + "y" * 40},
        "projects": {"proj": {"token": "main"}, "other": {}},
    })
    assert gs.token_for(settings, cfg, "proj") == "github_pat_" + "y" * 40
    assert gs.token_for(settings, cfg, "other") == "env-token-0123456789abcdef"
    assert gs.token_for(settings, cfg, "unknown") == "env-token-0123456789abcdef"

    # Removing a token detaches every project that used it.
    settings = gs.apply_patch(cfg, settings, {"remove_tokens": ["main"]})
    assert settings["projects"]["proj"]["token"] is None
    assert gs.token_for(settings, cfg, "proj") == "env-token-0123456789abcdef"


def test_policies_are_validated_and_bounded():
    cfg = _config()
    base = gs.normalize(None)
    with pytest.raises(ValueError, match="unknown source"):
        gs.apply_patch(cfg, base, {"projects": {"p": {"policies": {"nope": "auto"}}}})
    with pytest.raises(ValueError, match="mode"):
        gs.apply_patch(cfg, base, {"projects": {"p": {"policies": {"dependabot_prs": "yolo"}}}})
    with pytest.raises(ValueError, match="unknown token"):
        gs.apply_patch(cfg, base, {"projects": {"p": {"token": "ghost"}}})
    with pytest.raises(ValueError, match="does not look like"):
        gs.apply_patch(cfg, base, {"add_tokens": {"short": "abc"}})
    with pytest.raises(ValueError, match="public_url"):
        gs.apply_patch(cfg, base, {"public_url": "agent.example.com"})

    s = gs.apply_patch(cfg, base, {
        "poll_interval_min": 0, "public_url": "https://agent.example.com/v2/",
        "projects": {"p": {"policies": {"dependabot_prs": "auto"}, "budget_usd": 9999, "max_open_auto": 0, "authors": "bots"}},
    })
    assert s["poll_interval_min"] == 2
    assert s["public_url"] == "https://agent.example.com/v2"
    p = s["projects"]["p"]
    assert p["policies"]["dependabot_prs"] == "auto" and p["policies"]["ci_failures"] == "off"
    assert p["budget_usd"] == 200.0 and p["max_open_auto"] == 1 and p["authors"] == "bots"
    assert gs.enabled_projects(s) == ["p"]


def test_normalize_fills_every_gap_so_callers_never_branch_on_absence():
    s = gs.normalize({"projects": {"p": {"policies": {"security_alerts": "propose"}}}, "tokens": {"bad": {}}})
    assert s["tokens"] == {}                      # an entry without ciphertext is dropped
    assert s["projects"]["p"]["budget_usd"] == gs.DEFAULT_PROJECT["budget_usd"]
    assert set(s["projects"]["p"]["policies"]) == set(gs.SOURCES)
    assert s["notify"] == {"telegram": True, "email": False, "email_to": ""}


def test_code_scanning_inherits_the_dependabot_alert_mode_until_set_explicitly():
    # Projects configured before the source existed keep working the way the
    # operator expects: CodeQL alerts follow the Dependabot-alert policy.
    s = gs.normalize({"projects": {"p": {"policies": {"security_alerts": "propose"}},
                                   "q": {"policies": {"security_alerts": "auto", "code_scanning": "off"}},
                                   "r": {"policies": {"dependabot_prs": "auto"}}}})
    assert s["projects"]["p"]["policies"]["code_scanning"] == "propose"
    assert s["projects"]["q"]["policies"]["code_scanning"] == "off"        # explicit wins
    assert s["projects"]["r"]["policies"]["code_scanning"] == "off"        # nothing to inherit


def test_readme_documents_every_inbox_source():
    """The inbox table in the README is how an operator learns what the
    poller looks for. A source that exists only in code (2026-09-11:
    code_scanning) is a feature nobody knows they have."""
    from pathlib import Path

    readme = Path("README.md").read_text()
    missing = [meta["label"] for name, meta in gs.SOURCES.items() if meta["label"] not in readme]
    assert not missing, f"README GitHub inbox section omits source(s): {missing}"


def test_the_settings_card_can_render_every_source():
    """SOURCE_ORDER in the settings card is a hand-written array. A source
    added to the registry and not to it is invisible in the UI: the API sends
    it, the table never draws a column for it, and the operator cannot switch
    on a feature they have. See docs/playbooks/add-an-inbox-source.md."""
    import re
    from pathlib import Path

    tsx = Path("frontend/src/components/GitHubSettingsCard.tsx").read_text()
    m = re.search(r"SOURCE_ORDER[^=]*=\s*\[(.*?)\]", tsx, re.S)
    assert m, "SOURCE_ORDER is gone from GitHubSettingsCard.tsx -- has the card changed shape?"
    missing = [name for name in gs.SOURCES if f'"{name}"' not in m.group(1)]
    assert not missing, f"the settings card draws no column for: {missing}"


def test_the_frontend_type_knows_every_source():
    """GitHubSource is the union the whole frontend narrows on; a missing
    member makes the new source a type error at every use site."""
    from pathlib import Path

    api = Path("frontend/src/api.ts").read_text()
    line = next(ln for ln in api.splitlines() if "export type GitHubSource" in ln)
    missing = [name for name in gs.SOURCES if f'"{name}"' not in line]
    assert not missing, f"GitHubSource is missing: {missing}"
