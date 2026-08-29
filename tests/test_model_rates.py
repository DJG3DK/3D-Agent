"""Unit tests for the LLM router config-backed cost-estimation fallback
(agent/tools/model_rates.py) -- the path BudgetGuardMiddleware falls back to
now that LiteLLM's own cost annotation doesn't survive streaming (see
budget_guard.py's module docstring). Uses the real router config on this
host (not a fixture) -- this module has no fixture-injection point by design
(a single process-lifetime cache), so these tests exercise the actual file
the system reads in production rather than mocking the one thing that would
make a bug in the real file invisible.
"""

import pytest

import agent.tools.model_rates as model_rates


def test_estimate_cost_returns_zero_for_unknown_model():
    assert model_rates.estimate_cost("totally/made-up-model", 1000, 1000) == 0.0


def test_estimate_cost_returns_zero_for_none_model_name():
    assert model_rates.estimate_cost(None, 1000, 1000) == 0.0


def test_estimate_cost_computes_from_real_config_rates():
    # glm-5.2's real config rates: input_cost_per_token=0.00000119,
    # output_cost_per_token=0.00000374 (the router config's model_info for
    # "openrouter/z-ai/glm-5.2").
    cost = model_rates.estimate_cost("z-ai/glm-5.2", 1000, 500)
    expected = 1000 * 0.00000119 + 500 * 0.00000374
    assert cost == expected


def test_estimate_cost_scales_linearly_with_tokens():
    cost_1x = model_rates.estimate_cost("z-ai/glm-5.2", 1000, 1000)
    cost_2x = model_rates.estimate_cost("z-ai/glm-5.2", 2000, 2000)
    assert cost_2x == cost_1x * 2


def test_estimate_cost_zero_tokens_is_zero_cost():
    assert model_rates.estimate_cost("z-ai/glm-5.2", 0, 0) == 0.0


def test_rates_cache_populates_once_and_is_reused(monkeypatch):
    # Reset the module-level cache so this test controls exactly one load.
    monkeypatch.setattr(model_rates, "_rates", None)
    load_calls = []
    real_load = model_rates._load_rates

    def counting_load():
        load_calls.append(1)
        return real_load()

    monkeypatch.setattr(model_rates, "_load_rates", counting_load)

    model_rates.estimate_cost("z-ai/glm-5.2", 100, 100)
    model_rates.estimate_cost("z-ai/glm-5.2", 200, 200)
    model_rates.estimate_cost("amazon/nova-lite-v1", 100, 100)

    assert len(load_calls) == 1, "config.yaml should only be parsed once, then cached for the process lifetime"


# ---------------------------------------------------------------------------
# cache-read-aware pricing: a long tool-calling conversation resends nearly
# the same prefix every turn, so most of a later call's "input tokens" are
# cache reads, billed at a steep discount off the base input rate -- treating
# all of them as full-price input (the old behavior) overestimated real cost
# by several times on exactly this shape of conversation. The network fetch
# to OpenRouter's pricing endpoint is monkeypatched here (not hit for real)
# to keep this deterministic and independent of network access.
# ---------------------------------------------------------------------------


def test_cache_read_tokens_billed_at_the_discounted_rate(monkeypatch):
    monkeypatch.setattr(model_rates, "_rates", None)
    monkeypatch.setattr(model_rates, "_fetch_cache_read_rates", lambda: {"z-ai/glm-5.2": 0.00000026})

    cost = model_rates.estimate_cost("z-ai/glm-5.2", 118294, 376, cache_read_tokens=117888)
    fresh_input = 118294 - 117888
    expected = fresh_input * 0.00000119 + 117888 * 0.00000026 + 376 * 0.00000374
    assert cost == expected
    # Sanity check against the old (pre-fix) all-input-at-full-price
    # behavior -- the cache-aware cost must be substantially lower for this
    # shape of call, not a rounding-level difference.
    naive = 118294 * 0.00000119 + 376 * 0.00000374
    assert cost < naive * 0.3


def test_cache_read_tokens_clamped_to_input_tokens(monkeypatch):
    monkeypatch.setattr(model_rates, "_rates", None)
    monkeypatch.setattr(model_rates, "_fetch_cache_read_rates", lambda: {"z-ai/glm-5.2": 0.00000026})

    # A malformed/inconsistent usage report (cache_read > input_tokens)
    # must never go negative on the "fresh" portion.
    cost = model_rates.estimate_cost("z-ai/glm-5.2", 100, 50, cache_read_tokens=9999)
    expected = 100 * 0.00000026 + 50 * 0.00000374
    assert cost == expected


def test_model_without_published_cache_rate_falls_back_to_full_input_price(monkeypatch):
    monkeypatch.setattr(model_rates, "_rates", None)
    monkeypatch.setattr(model_rates, "_fetch_cache_read_rates", lambda: {})  # no OpenRouter data for this model

    cost = model_rates.estimate_cost("z-ai/glm-5.2", 1000, 500, cache_read_tokens=800)
    expected = 1000 * 0.00000119 + 500 * 0.00000374  # cache tokens charged at full input rate, no discount
    assert cost == expected


def test_real_openrouter_fetch_returns_a_well_formed_rate_table():
    """One real network call against OpenRouter's actual pricing endpoint,
    matching this project's own preference for verifying against real infra
    over mocking the one thing that would make a real-world format change
    invisible. Only checks shape/plausibility, not exact values (OpenRouter's
    published prices can change), so this doesn't get flaky over a routine
    price update.
    """
    rates = model_rates._fetch_cache_read_rates()
    assert isinstance(rates, dict)
    if not rates:
        pytest.skip("OpenRouter pricing endpoint unreachable from this environment")
    assert "z-ai/glm-5.3" in rates
    assert 0 < rates["z-ai/glm-5.3"] < 0.00001  # plausible per-token dollar range, not a unit-mixup
