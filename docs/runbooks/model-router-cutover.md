# Cutting over to the in-house model router

Written 2026-09-15. Everything below was executed against the running system
except the cutover step itself, which is the one thing left.

The new router runs beside LiteLLM on its own port, reading the **same**
`services/llm-router/config.yaml`. Cutover is one environment variable, and so
is rollback.

---

## What was verified before writing this

Against the live config, real models, real money — the total spend across all
of it was under two cents.

| check | result |
|---|---|
| Plain completion, `agent-coder` | content identical to LiteLLM's |
| Tool call through the real `llm_for_role` client | `get_weather({'city': 'Paris'})` — tool calls survive the round trip |
| Streaming through the real client | 34 chunks, correct text, usage scraped from the final chunk |
| `budget_guard.call_id_of(msg)` | reads our `x-litellm-call-id` |
| `RouterLedger.actual_costs()` | resolved every call id to a billed cost |
| `metrics._role_and_model()` | attributes role and model from our ledger lines |
| `agent/health.py:_check_router` | `{'ok': True}` |
| Fallback chain | bogus primary failed, `agent-coder` answered, `x-router-attempt: 2`, both attempts in the ledger |
| Hot reload | repin applied with **no restart**, same PID, next call served by the new model |
| 20 concurrent calls | 20/20, 20 unique call ids, every line carrying a cost |
| Our own overhead | **1.6 ms** median; client-side latency indistinguishable from LiteLLM |
| Unit tests | 48 passing, no network |

## Already done

The service is **running under pm2 on port 4001** and saved to the pm2 process
list, reading the same `config.yaml`. Nothing points at it, so this is not a
cutover -- it is a warm service with a real call already through it
(`task_id: pm2-smoke` in the ledger, readable by `metrics._role_and_model`).

```
pm2 list                       # model-router, online
curl -s localhost:4001/health/readiness
curl -s localhost:4001/v1/stats?window_minutes=60 -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Footprint next to the thing it replaces: **57 MB against 815 MB**.

## Cutover

1. **Point the agent at it.** In `/home/3d-agent/.env`:

   ```
   LITELLM_BASE_URL=http://127.0.0.1:4001/v1
   ```

   Then `pm2 restart tektonix`. Do this while **no task is running** — the
   restart kills the current work pass, which is a property of the agent, not
   of either router.

2. **Watch one real task end to end.** The things to see: a task's cost moving
   on the dashboard (proves the ledger round trip), and a tool-calling step
   completing (proves the buffered path).

3. **Then the other consumers**, one at a time, same variable:
   `services/commit-reviewer`, the mail agent, the trading bot's gate.

## Rollback

Put `LITELLM_BASE_URL` back to `http://127.0.0.1:4000/v1` and restart the
agent. LiteLLM keeps running throughout — nothing about it is changed or
removed by this, and both read the same config, so no state diverges.

Decommission the proxy only after a few days of the new one carrying real
traffic.

## What to watch in the first day

* **`provider` in the ledger** is new. If one provider shows up on every slow
  call, that is worth knowing and was previously invisible.
* **`attempt: 2` rows** mean a fallback fired. Under LiteLLM these were only
  visible as a gap.
* **Streaming falls back only before the first byte.** A refusal (429, 5xx)
  retries and falls back like any other call. A failure part-way through a
  stream ends the response rather than splicing a second completion onto a
  partial one — the client sees truncation, which is the honest outcome.

## Known differences from LiteLLM

* No admin UI. The dashboard's Models page is the real admin surface and is
  unaffected; the LiteLLM UI behind the passkey gate goes away with the proxy.
* The tier system, `smart-router` and `reasoning-tier` are not implemented.
  They were removed from the config on 2026-09-13 — one call in the ledger's
  entire history — so there is nothing to port.
* Load balancing across duplicate `model_name` entries is not implemented.
  Nothing has used it since the tier pools were deleted; a duplicate alias logs
  a warning and the first wins.
