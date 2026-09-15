"""The router's HTTP surface.

Deliberately small. Three routes are all anything in this deployment calls:

  POST /v1/chat/completions   every model call, from four services
  GET  /health/liveliness     agent/health.py, unauthenticated by design
  GET  /v1/model/info         the deployment table, for tooling

The compatibility contract is not a matter of taste -- each item below is
something that breaks a specific caller if it changes:

  * `x-litellm-call-id` on every response. agent/middleware/budget_guard.py
    reads that exact header name (CALL_ID_HEADER) to match a model call to its
    billed cost in the ledger. Renaming it silently reverts every task to
    estimated spend.
  * `metadata.agent_task_id` / `agent_session_id` in the request body, put
    there by deep_agent._call_metadata, recorded and NOT forwarded upstream.
    Without them the ledger can price a call but never total a task.
  * Bearer auth against the same master key the proxy used.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router import ledger, upstream
from router.config import Registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("model-router")

MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("MODEL_ROUTER_KEY") or ""
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# Kept as the literal litellm spelling: budget_guard matches on it.
CALL_ID_HEADER = "x-litellm-call-id"

registry = Registry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled client for the process. Connection reuse is most of the
    # difference between a 200ms call and a 700ms one at this volume.
    app.state.http = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
        headers={"HTTP-Referer": "https://agent.3dcryptobots.com", "X-Title": "3D-Agent"},
    )
    logger.info("model-router up: %d deployments", len(registry.table.deployments))
    yield
    await app.state.http.aclose()


app = FastAPI(title="3D-Agent model router", lifespan=lifespan)


def _authorise(authorization: str | None) -> None:
    if not MASTER_KEY:
        return  # unset means an unguarded local dev run, same as the proxy
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization.split(" ", 1)[1].strip() != MASTER_KEY:
        raise HTTPException(401, "invalid token")


@app.get("/health/liveliness")
async def liveliness():
    """Unauthenticated on purpose: agent/health.py polls it with no key and no
    model call, and a health probe that needs a secret is a health probe that
    stops being run."""
    t = registry.table
    return {"status": "alive", "deployments": len(t.deployments), "config_loaded_at": t.loaded_at}


@app.get("/health/readiness")
async def readiness():
    t = registry.table
    ok = bool(t.deployments) and bool(OPENROUTER_KEY)
    return JSONResponse({"status": "ready" if ok else "degraded",
                         "deployments": len(t.deployments),
                         "upstream_key": bool(OPENROUTER_KEY)},
                        status_code=200 if ok else 503)


@app.get("/v1/model/info")
async def model_info(authorization: str | None = Header(default=None)):
    _authorise(authorization)
    t = registry.table
    return {"data": [
        {"model_name": d.alias,
         "litellm_params": {"model": f"openrouter/{d.model}"},
         "model_info": {"input_cost_per_token": d.input_cost_per_token,
                        "output_cost_per_token": d.output_cost_per_token,
                        "timeout_s": d.timeout_s}}
        for d in t.deployments.values()]}


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    _authorise(authorization)
    t = registry.table
    return {"object": "list",
            "data": [{"id": a, "object": "model", "owned_by": "3d-agent"} for a in t.deployments]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    _authorise(authorization)
    body = await request.json()
    alias = body.get("model")
    if not alias:
        raise HTTPException(400, "no model given")

    table = registry.table
    if alias not in table.deployments:
        raise HTTPException(404, f"unknown model {alias!r}")

    meta = body.get("metadata") or {}
    task_id = meta.get("agent_task_id")
    session_id = meta.get("agent_session_id")
    call_id = str(uuid.uuid4())
    client: httpx.AsyncClient = request.app.state.http

    if body.get("stream"):
        return await _streamed(client, table, alias, body, call_id, task_id, session_id)

    # Buffered: walk the fallback chain, first success wins.
    chain = table.chain(alias)
    last: upstream.Attempt | None = None
    for attempt_no, name in enumerate(chain, start=1):
        dep = table.deployments[name]
        att = await upstream.call_once(client, OPENROUTER_KEY, body, dep.model,
                                       dep.extra_body, dep.timeout_s)
        att.alias = name
        last = att
        ledger.record(
            call_id=call_id, alias=alias, model=att.usage.model or dep.model,
            prompt_tokens=att.usage.prompt_tokens, completion_tokens=att.usage.completion_tokens,
            cached_tokens=att.usage.cached_tokens, cost=att.usage.cost,
            duration_s=att.duration_s, task_id=task_id, session_id=session_id,
            provider=att.usage.provider, attempt=attempt_no,
            error=not att.ok, error_detail=att.error,
        )
        if att.ok:
            headers = {CALL_ID_HEADER: call_id, "x-router-deployment": name,
                       "x-router-attempt": str(attempt_no)}
            return JSONResponse(att.payload, headers=headers)
        logger.warning("attempt %d/%d on %s failed: %s", attempt_no, len(chain), name, att.error)

    detail = last.error if last else "no deployment answered"
    return JSONResponse({"error": {"message": detail, "type": "upstream_error",
                                   "chain": chain, "call_id": call_id}},
                        status_code=502, headers={CALL_ID_HEADER: call_id})


async def _streamed(client, table, alias, body, call_id, task_id, session_id):
    """Streaming has no fallback, deliberately.

    Once a byte has reached the client the response is committed; retrying on
    another deployment would splice two different completions together. The
    first deployment either works or the stream ends with an error the caller
    can see. Buffered calls -- which is everything tool-calling, and so nearly
    everything the agent does -- keep the full chain.
    """
    dep = table.deployments[alias]
    usage = upstream.Usage()
    started = time.monotonic()

    async def body_iter():
        try:
            async for chunk in upstream.stream_once(client, OPENROUTER_KEY, body, dep.model,
                                                    dep.extra_body, dep.timeout_s, usage):
                yield chunk
        except Exception as e:  # noqa: BLE001
            logger.warning("stream failed on %s: %s", alias, e)
            ledger.record(call_id=call_id, alias=alias, model=dep.model, duration_s=time.monotonic() - started,
                          task_id=task_id, session_id=session_id, error=True,
                          error_detail=f"{type(e).__name__}: {str(e)[:300]}")
            return
        ledger.record(
            call_id=call_id, alias=alias, model=usage.model or dep.model,
            prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
            cached_tokens=usage.cached_tokens, cost=usage.cost,
            duration_s=time.monotonic() - started, task_id=task_id, session_id=session_id,
            provider=usage.provider,
        )

    return StreamingResponse(body_iter(), media_type="text/event-stream",
                             headers={CALL_ID_HEADER: call_id, "x-router-deployment": alias,
                                      "cache-control": "no-cache"})
