"""The deployment table, and the reload that litellm could not do.

litellm read config.yaml once at startup. Repinning a model from the dashboard
therefore meant restarting the router, and a restart kills every model call in
flight across every service sharing it -- the operation docs/architecture.md
tells you never to perform while a task is running. These tests exist to keep
that property from quietly coming back.
"""

from __future__ import annotations

import time

import pytest
import yaml

from router.config import Registry, load

BASE = {
    "model_list": [
        {"model_name": "agent-coder",
         "litellm_params": {"model": "openrouter/deepseek/deepseek-v4.1-flash",
                            "extra_body": {"provider": {"require_parameters": True}}},
         "model_info": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07}},
        {"model_name": "claude-haiku-4.5",
         "litellm_params": {"model": "openrouter/anthropic/claude-haiku-4.5", "timeout": 120}},
    ],
    "router_settings": {"fallbacks": [{"agent-coder": ["claude-haiku-4.5", "nonexistent"]}]},
}


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(BASE))
    return p


def test_aliases_resolve_to_their_model(cfg):
    t = load(cfg)
    assert t.resolve("agent-coder").model == "deepseek/deepseek-v4.1-flash"


def test_the_openrouter_prefix_is_stripped(cfg):
    """litellm used the leading segment to choose an SDK. We only ever talk to
    OpenRouter, and it does not want the prefix in the model id."""
    assert not t_model(cfg).startswith("openrouter/")


def t_model(cfg):
    return load(cfg).resolve("agent-coder").model


def test_extra_body_and_costs_survive(cfg):
    d = load(cfg).resolve("agent-coder")
    assert d.extra_body == {"provider": {"require_parameters": True}}
    assert d.input_cost_per_token == 1.5e-07


def test_a_per_deployment_timeout_is_honoured(cfg):
    """Exactly one deployment had a timeout under litellm; the rest had none,
    which is how an upstream call ran 1802 seconds for 280 output tokens."""
    assert load(cfg).resolve("claude-haiku-4.5").timeout_s == 120.0
    assert load(cfg).resolve("agent-coder").timeout_s > 0


def test_the_fallback_chain_starts_with_the_alias_itself(cfg):
    assert load(cfg).chain("agent-coder")[0] == "agent-coder"


def test_a_fallback_naming_a_missing_deployment_is_dropped(cfg):
    """litellm answered that case at the moment of failure with 'Available
    Model Group Fallbacks=None' -- the worst possible time to discover it."""
    assert load(cfg).chain("agent-coder") == ["agent-coder", "claude-haiku-4.5"]


def test_an_alias_with_no_fallbacks_is_just_itself(cfg):
    assert load(cfg).chain("claude-haiku-4.5") == ["claude-haiku-4.5"]


# ---------------------------------------------------------------------------
# hot reload
# ---------------------------------------------------------------------------

def test_a_repin_takes_effect_without_a_restart(cfg):
    reg = Registry(cfg)
    assert reg.table.resolve("agent-coder").model == "deepseek/deepseek-v4.1-flash"

    changed = {**BASE}
    changed["model_list"] = [dict(m) for m in BASE["model_list"]]
    changed["model_list"][0] = {**changed["model_list"][0],
                                "litellm_params": {"model": "openrouter/z-ai/glm-5.3"}}
    time.sleep(0.01)
    cfg.write_text(yaml.safe_dump(changed))

    assert reg.table.resolve("agent-coder").model == "z-ai/glm-5.3"


def test_an_unchanged_file_is_not_reparsed(cfg):
    reg = Registry(cfg)
    first = reg.table
    assert reg.table is first, "same object: a stat, not a parse, on the hot path"


def test_a_broken_config_keeps_the_running_table(cfg):
    """The moment the operator saves a bad file is exactly the moment to keep
    serving the table that is known to work."""
    reg = Registry(cfg)
    good = reg.table.resolve("agent-coder").model
    time.sleep(0.01)
    cfg.write_text("model_list: [this is not: valid: yaml")

    assert reg.table.resolve("agent-coder").model == good


def test_a_broken_config_is_not_reparsed_every_request(cfg):
    """Remembering the bad mtime matters: without it every single request
    re-parses a file that is still broken."""
    reg = Registry(cfg)
    time.sleep(0.01)
    cfg.write_text("{{{")
    _ = reg.table          # the reload attempt is the point
    assert reg._table.mtime == cfg.stat().st_mtime


def test_a_duplicate_alias_keeps_the_first(cfg):
    doubled = {**BASE, "model_list": BASE["model_list"] + [
        {"model_name": "agent-coder", "litellm_params": {"model": "openrouter/other/model"}}]}
    time.sleep(0.01)
    cfg.write_text(yaml.safe_dump(doubled))
    assert load(cfg).resolve("agent-coder").model == "deepseek/deepseek-v4.1-flash"


def test_entries_without_a_model_are_skipped(cfg, tmp_path):
    p = tmp_path / "partial.yaml"
    p.write_text(yaml.safe_dump({"model_list": [
        {"model_name": "broken"},
        {"litellm_params": {"model": "openrouter/x/y"}},
        {"model_name": "fine", "litellm_params": {"model": "openrouter/x/y"}},
    ]}))
    t = load(p)
    assert list(t.deployments) == ["fine"]
