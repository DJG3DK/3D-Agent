"""Per-model $/token rate table, loaded once from the LLM router's own
config.yaml -- the same file that already defines these rates for the
proxy's own billing, so there is exactly one place the base input/output
rates are maintained.

Only used as a fallback for BudgetGuardMiddleware's cost read. LiteLLM's own
computed response_metadata["token_usage"]["cost"] is preferred when present
(it's the proxy's own exact billed cost), but that field is absent entirely
on every call that goes through agent.astream_events(..., version="v3") --
OpenAI-compatible streaming responses carry standard token counts
(usage_metadata) but not LiteLLM's extra cost annotation, regardless of
stream_usage/stream_options. This computes cost from
usage_metadata.input_tokens/output_tokens against this table instead.

Cache-aware: many OpenRouter-routed models bill a cached-prompt-token read
at a steep discount off the base input rate (confirmed against OpenRouter's
own public pricing: one pinned model's cache-read rate is roughly 1/5th its
base input rate). A long tool-calling conversation resends nearly the same
prefix on every turn, so the overwhelming majority of a later call's "input
tokens" are cache reads, not fresh ones -- treating all of them as full-price
input (as a naive per-token calculation does) can overestimate real cost by
several times on exactly the kind of long, looping conversation this exists
to catch. config.yaml only carries the two base rates, so the cache-read
rate is fetched from OpenRouter's own public, unauthenticated pricing
endpoint instead, once per process lifetime, and merged in alongside them.
"""

import logging
from pathlib import Path

import os

import httpx
import yaml

logger = logging.getLogger("3d-agent")

# Repo-relative, not an absolute /home path (audit C-1): the hard-coded path
# meant the entire budget ceiling silently did not exist on any box where the
# repo lived elsewhere -- warm_rates() raised FileNotFoundError at startup
# (swallowed) and every task then died on its first model call. This file is
# agent/tools/model_rates.py, so the config is three parents up.
LLM_ROUTER_CONFIG_PATH = Path(
    os.environ.get("LLM_ROUTER_CONFIG_PATH")
    or (Path(__file__).resolve().parents[2] / "services" / "llm-router" / "config.yaml")
)
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_rates: dict[str, dict[str, float]] | None = None


def _fetch_cache_read_rates() -> dict[str, float]:
    """{model_id: cache_read_cost_per_token}, from OpenRouter's public
    models listing. Best-effort: an unreachable endpoint or a model without
    published cache pricing just means no discount is applied for it --
    estimate_cost falls back to charging cached tokens at the full input
    rate, which overestimates but never underestimates against the budget
    ceiling.
    """
    try:
        resp = httpx.get(OPENROUTER_MODELS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 -- pricing metadata, never worth failing a task over
        logger.warning("OpenRouter pricing fetch failed: %s", e)
        return {}
    rates: dict[str, float] = {}
    for entry in data.get("data", []):
        model_id = entry.get("id")
        cache_read = (entry.get("pricing") or {}).get("input_cache_read")
        if model_id and cache_read is not None:
            try:
                rates[model_id] = float(cache_read)
            except (TypeError, ValueError):
                pass
    return rates


def _load_rates() -> dict[str, dict[str, float]]:
    """{model_key: {"input": ..., "output": ..., "cache_read": ...}}, keyed
    both by the raw model id litellm returns in response_metadata["model_name"]
    (return_raw_model_name: true strips config.yaml's own "openrouter/"
    prefix off litellm_params.model -- mirror that here so lookups match)
    and by the config alias (pinned-role calls echo the alias, not the raw
    id -- see the alias branch below).
    """
    rates: dict[str, dict[str, float]] = {}
    cfg = yaml.safe_load(LLM_ROUTER_CONFIG_PATH.read_text())
    cache_read_rates = _fetch_cache_read_rates()
    for entry in cfg.get("model_list", []):
        litellm_params = entry.get("litellm_params") or {}
        model_info = entry.get("model_info") or {}
        raw = litellm_params.get("model", "")
        stripped = raw.split("/", 1)[1] if raw.startswith("openrouter/") else raw
        input_cost = model_info.get("input_cost_per_token")
        output_cost = model_info.get("output_cost_per_token")
        if stripped and input_cost is not None and output_cost is not None:
            entry_rates = {
                "input": float(input_cost),
                "output": float(output_cost),
                "cache_read": cache_read_rates.get(stripped, float(input_cost)),
            }
            rates[stripped] = entry_rates
            # Also key by the config alias (model_name): pinned-alias calls
            # echo the alias as the response's model_name --
            # return_raw_model_name only applies to auto_router deployments.
            # Without this, every pinned-role call would fall through the
            # raw-id lookup and be costed at $0.0.
            alias = entry.get("model_name")
            if alias and alias not in rates:
                rates[alias] = entry_rates
    return rates


async def warm_rates() -> None:
    """Pre-loads the rate table (including the OpenRouter network fetch) in
    a background thread at server startup, so the first real cost estimate
    doesn't block the event loop on a synchronous network call.
    """
    global _rates
    if _rates is None:
        import asyncio

        _rates = await asyncio.to_thread(_load_rates)


def estimate_cost(
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Returns 0.0 (not a guess in either direction) if model_name is
    missing or unrecognized -- an unknown model is a signal to add it to
    llm-router/config.yaml's model_info, not a reason to estimate blind.

    `cache_read_tokens` (from usage_metadata.input_token_details.cache_read)
    is billed at its own discounted rate, not the full input rate -- see
    this module's own docstring for why that distinction matters.
    """
    global _rates
    if _rates is None:
        _rates = _load_rates()
    if not model_name:
        return 0.0
    rate = _rates.get(model_name)
    if rate is None:
        return 0.0  # best-effort caller (analytics); the budget guard uses estimate_cost_strict
    cache_read_tokens = min(cache_read_tokens, input_tokens)
    fresh_input_tokens = input_tokens - cache_read_tokens
    return (
        fresh_input_tokens * rate["input"]
        + cache_read_tokens * rate["cache_read"]
        + output_tokens * rate["output"]
    )


class UnpricedModelError(Exception):
    """A model with no rate in llm-router/config.yaml was billed against a hard
    budget ceiling. For a SPEND ceiling, "unknown price" and "free" must not be
    the same value -- 200 calls at ~1.4M tokens once tracked as $0.00 against a
    $5 ceiling that never tripped (audit C-1). Raised by estimate_cost_strict so
    the budget guard fails safe instead of undercounting to zero."""


def estimate_cost_strict(
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Like estimate_cost, but raises UnpricedModelError when the model has no
    known rate, so a hard budget ceiling can never be defeated by an unpriced
    model reading as $0. Callers that only want a best-effort dollar figure
    (analytics) keep using estimate_cost."""
    global _rates
    if _rates is None:
        _rates = _load_rates()
    if model_name and _rates.get(model_name) is not None:
        return estimate_cost(model_name, input_tokens, output_tokens, cache_read_tokens)
    raise UnpricedModelError(
        f"model {model_name!r} has no rate in llm-router/config.yaml model_info; "
        f"add it so its spend counts against the budget ceiling"
    )
