# model-router

The agent's own router. Replaces the LiteLLM proxy.

```
./venv/bin/uvicorn router.app:app --host 127.0.0.1 --port 4001
pm2 start ecosystem.config.js          # the managed form
```

## Why it exists

Every one of the 21 deployments in `config.yaml` is `openrouter/...`, so
LiteLLM was a proxy in front of a proxy: its headline feature, normalising many
providers behind one OpenAI-compatible API, is a job OpenRouter already does.
What we actually used was alias resolution, ordered fallbacks, a billed-cost
figure and a callback writing our own ledger — about 600 lines of the 700-line
config's worth of machinery.

Two things it does that the proxy structurally could not:

**Hot reload.** LiteLLM reads its config once at startup, so repinning a model
from the dashboard required restarting the router — which kills every model
call in flight across every service sharing it. `docs/architecture.md` carries
the warning: *"never restart this while a task is mid-call."* Here the file's
mtime is checked per request and a changed table is swapped in between
requests. Measured: repin applied, same PID, next call served by the new model.

**Per-alias timeouts.** Exactly one deployment had a timeout under LiteLLM. That
is how a coder call sat upstream for 1,802 seconds and returned 280 tokens.

It also runs on current FastAPI. The proxy pinned `fastapi==0.140.6` because
litellm 1.96.2 imported a private helper that 0.140.7 removed.

## Compatibility

It reads the **same `services/llm-router/config.yaml`** — the operator's pins,
the file the Models page writes. There is no migration.

Four things are contracts, not choices, each with a caller that breaks:

| contract | who depends on it |
|---|---|
| `x-litellm-call-id` response header | `budget_guard.call_id_of` matches spend on this exact name |
| `metadata.agent_task_id` in the body | without it the ledger prices a call but cannot total a task |
| `routing.jsonl` field names | `router_ledger.py`, `metrics.py`, `model_rates.py` all parse it |
| `/health/liveliness`, unauthenticated | `agent/health.py` polls it with no key |

## Endpoints

| route | notes |
|---|---|
| `POST /v1/chat/completions` | streaming and buffered; fallbacks on the buffered path |
| `GET /health/liveliness` | no auth, by design |
| `GET /health/readiness` | 503 when there are no deployments or no upstream key |
| `GET /v1/model/info` | the deployment table |
| `GET /v1/models` | alias list |

## Behaviour worth knowing

**Fallbacks apply up to the first byte**, on both paths. A response is
committed the moment a byte reaches the client, and an upstream refusing with a
429 does so before any body exists — so falling back there is as safe for a
stream as for a buffered call. Only a failure *part-way through* a stream is
unrecoverable, because retrying would splice two completions into one
response.

**Every attempt is a ledger line**, sharing one `call_id`. A fallback therefore
cannot double-charge a task, and the Analytics error rate sees the failure that
actually happened.

**Costs are OpenRouter's `usage.cost`**, never computed from the rate table in
`config.yaml`. The estimate and the bill disagree; that is why the ledger
exists.

**A broken config keeps the running table.** The moment an operator saves a bad
file is exactly the moment to keep serving the one known to work.

## Tests

```
./venv/bin/python -m pytest tests/ -q      # 48, no network
```

Live checks live in `docs/runbooks/model-router-cutover.md`.
