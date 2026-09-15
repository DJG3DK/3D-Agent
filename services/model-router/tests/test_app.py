"""The HTTP surface, and the compatibility contract it has to keep.

Every assertion here corresponds to a specific caller that breaks if it
changes. They are not style preferences:

  * `x-litellm-call-id` is read by name in agent/middleware/budget_guard.py
    (CALL_ID_HEADER). Rename it and every task silently reverts from billed
    spend to estimated spend -- with no error anywhere.
  * `metadata.agent_task_id` is put in the body by deep_agent._call_metadata.
    Drop it and the ledger can price a call but never total a task, which is
    how a killed pass used to lose the money it had spent.
  * /health/liveliness is polled by agent/health.py with no credentials.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from router import app as app_module
from router import upstream
from router.config import Registry

CONFIG = {
    "model_list": [
        {"model_name": "agent-coder", "litellm_params": {"model": "openrouter/deepseek/flash"}},
        {"model_name": "backup", "litellm_params": {"model": "openrouter/anthropic/haiku"}},
    ],
    "router_settings": {"fallbacks": [{"agent-coder": ["backup"]}]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(CONFIG))
    monkeypatch.setattr(app_module, "registry", Registry(cfg))
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-test")
    monkeypatch.setattr(app_module.ledger, "LOG_PATH", tmp_path / "ledger.jsonl")
    with TestClient(app_module.app) as c:
        yield c


def _ok(model="deepseek/flash", cost=0.001):
    return {"id": "gen-1", "model": model, "provider": "TestProvider",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": cost,
                      "prompt_tokens_details": {"cached_tokens": 4}}}


def _stub(monkeypatch, results):
    """results: list of (ok, payload_or_error) consumed in order."""
    calls = []

    async def fake(client, api_key, body, model, extra_body, timeout_s):
        calls.append({"model": model, "body": body, "timeout": timeout_s})
        ok, data = results[len(calls) - 1]
        if ok:
            return upstream.Attempt(alias="", model=model, ok=True, status=200,
                                    duration_s=0.1, payload=data,
                                    usage=upstream.Usage.from_payload(data))
        return upstream.Attempt(alias="", model=model, ok=False, status=500,
                                duration_s=0.1, error=str(data))

    monkeypatch.setattr(app_module.upstream, "call_once", fake)
    return calls


def _post(client, **over):
    body = {"model": "agent-coder", "messages": [{"role": "user", "content": "hi"}]}
    body.update(over)
    return client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": "Bearer sk-test"})


# ---------------------------------------------------------------------------
# auth and routing
# ---------------------------------------------------------------------------

def test_liveliness_needs_no_key(client):
    r = client.get("/health/liveliness")
    assert r.status_code == 200 and r.json()["status"] == "alive"


def test_a_bad_key_is_rejected(client):
    r = client.post("/v1/chat/completions", json={"model": "agent-coder", "messages": []},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_a_missing_key_is_rejected(client):
    r = client.post("/v1/chat/completions", json={"model": "agent-coder", "messages": []})
    assert r.status_code == 401


def test_an_unknown_alias_is_404_not_a_wasted_upstream_call(client, monkeypatch):
    calls = _stub(monkeypatch, [])
    r = _post(client, model="no-such-alias")
    assert r.status_code == 404
    assert calls == [], "an unknown alias must not reach the provider"


def test_the_alias_resolves_to_its_model(client, monkeypatch):
    calls = _stub(monkeypatch, [(True, _ok())])
    assert _post(client).status_code == 200
    assert calls[0]["model"] == "deepseek/flash"


# ---------------------------------------------------------------------------
# the compatibility contract
# ---------------------------------------------------------------------------

def test_the_call_id_header_is_the_name_budget_guard_reads(client, monkeypatch):
    _stub(monkeypatch, [(True, _ok())])
    r = _post(client)
    assert app_module.CALL_ID_HEADER == "x-litellm-call-id"
    assert r.headers.get("x-litellm-call-id")


def test_the_ledger_line_matches_the_call_id_header(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok())])
    r = _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["call_id"] == r.headers["x-litellm-call-id"], (
        "budget_guard matches spend on exactly this pairing")


def test_the_task_id_is_recorded_and_not_forwarded(client, monkeypatch, tmp_path):
    calls = _stub(monkeypatch, [(True, _ok())])
    _post(client, metadata={"agent_task_id": "T1", "agent_session_id": "S1"})
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["task_id"] == "T1" and row["session_id"] == "S1"
    sent = upstream.build_body(calls[0]["body"], "m", {})
    assert "metadata" not in sent, "our routing metadata is not the provider's business"


def test_the_billed_cost_is_the_providers_not_a_rate_table(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok(cost=0.0424))])
    _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["cost"] == 0.0424
    assert row["cached_tokens"] == 4


# ---------------------------------------------------------------------------
# fallbacks
# ---------------------------------------------------------------------------

def test_a_failure_falls_through_to_the_next_deployment(client, monkeypatch):
    calls = _stub(monkeypatch, [(False, "upstream exploded"), (True, _ok("anthropic/haiku"))])
    r = _post(client)
    assert r.status_code == 200
    assert [c["model"] for c in calls] == ["deepseek/flash", "anthropic/haiku"]
    assert r.headers["x-router-attempt"] == "2"
    assert r.headers["x-router-deployment"] == "backup"


def test_both_attempts_are_recorded(client, monkeypatch, tmp_path):
    """The failed attempt is part of what happened, and the Analytics error
    rate is only honest if it is written down."""
    _stub(monkeypatch, [(False, "boom"), (True, _ok("anthropic/haiku"))])
    _post(client)
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["error"] is True and rows[0]["attempt"] == 1
    assert rows[1].get("error") is None and rows[1]["attempt"] == 2


def test_the_whole_chain_failing_is_a_502_with_the_chain_named(client, monkeypatch):
    _stub(monkeypatch, [(False, "a"), (False, "b")])
    r = _post(client)
    assert r.status_code == 502
    assert r.json()["error"]["chain"] == ["agent-coder", "backup"]
    assert r.headers.get("x-litellm-call-id")


def test_one_ledger_line_per_attempt_shares_the_call_id(client, monkeypatch, tmp_path):
    """One logical call, one id -- so a fallback cannot be double-charged."""
    _stub(monkeypatch, [(False, "a"), (True, _ok())])
    r = _post(client)
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert {row["call_id"] for row in rows} == {r.headers["x-litellm-call-id"]}


# ---------------------------------------------------------------------------
# the table endpoints
# ---------------------------------------------------------------------------

def test_model_info_reports_the_openrouter_form(client):
    r = client.get("/v1/model/info", headers={"Authorization": "Bearer sk-test"})
    names = {m["model_name"]: m["litellm_params"]["model"] for m in r.json()["data"]}
    assert names["agent-coder"] == "openrouter/deepseek/flash"


def test_model_info_needs_a_key(client):
    assert client.get("/v1/model/info").status_code == 401
