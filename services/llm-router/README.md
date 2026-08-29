# llm-router

A [LiteLLM](https://github.com/BerriAI/litellm) proxy that every model call on this box goes through.

Roles are **named aliases**, not model ids. `agent-coder`, `agent-reviewer`, `agent-consolidator`
and the rest resolve here, so changing which model a role uses is a config edit plus a restart — no
code change in any of the services that call it.

## Why the indirection is worth it

- **Swappable from a dashboard.** 3D-Agent's Models tab rewrites the `model:` line of these
  `agent-*` entries directly. Nothing else in the file is touched.
- **Costs in one place.** Each pin carries `model_info` rates, so spend is attributable per role
  rather than per raw model id.
- **Everything is logged.** `custom_callbacks.routing_logger` records which model actually answered
  each request. A service calling a provider directly is invisible to that — which is exactly what
  happened with the commit reviewer for months, hardcoded to a model id and bypassing this proxy
  entirely, so its spend appeared nowhere.

## Provider routing

Every `agent-*` pin carries:

```yaml
extra_body: {"provider": {"require_parameters": true}}
```

OpenRouter fans a model out across several upstream providers and **they do not all support the
same request parameters**. `require_parameters` routes only to providers that support every
parameter in the request — per-request, so a call with tools and reasoning is constrained while a
plain completion is not, and there is no static provider allow-list to maintain.

This was added after a real failure: the memory consolidator sends structured output, which is a
forced tool call underneath, and Alibaba rejects `tool_choice=required/object` while the model is in
thinking mode. Nightly consolidation failed silently for months for every project that actually had
work to do. The guard did not fix it — it made it **honest**, turning a silent 400 into a
`404 No endpoints found that support the provided 'tool_choice' value`, which is what exposed the
real problem.

Two caveats worth knowing:

- **A catalog cannot answer this.** `qwen3.8-max` advertises `tools`, `tool_choice`,
  `structured_outputs` *and* `reasoning` — byte-identical to a model that works. `supported_parameters`
  is a flat union across providers and cannot express a refused *combination*.
- **Compliance is per-provider, and OpenRouter load-balances.** The same model can pass one request
  and fail the next. For a role that must not break, pin the provider explicitly (`provider.order`
  / `provider.ignore`, as the `Venice` exclusions here already do).

`router_settings.fallbacks` gives the consolidator a second chance, since a single 4xx used to end
that project's nightly run entirely.

## Layout

```
config.yaml            model list, aliases, rates, provider routing, fallbacks
custom_callbacks.py    routing logger — powers the dashboard's Router tab
ecosystem.config.js    pm2
```

Listens on `0.0.0.0:4000`. Callers authenticate with `LITELLM_MASTER_KEY`.

Setup: `cp .env.example .env`, fill in the OpenRouter key and generate a master key
(`openssl rand -hex 32`). The same master key goes in the agent's `.env` as `LITELLM_API_KEY`.
The `model_list` in `config.yaml` ships with this deployment's pins as a worked example — the
`agent-*` aliases are the contract; what each resolves to is yours to change.
