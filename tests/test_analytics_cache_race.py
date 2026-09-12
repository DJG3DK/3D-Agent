"""The cold-start race that the analytics caches used to have is now absent
by construction, because the caches are gone.

History worth keeping: those three panels were served from LangSmith, a paged
remote query slow enough to need serve-stale-while-revalidate. The caches used
a bare boolean "refreshing" flag, so a second caller saw "already refreshing"
and read an empty cache instead of waiting -- confirmed live in 2026-08-24
(trace-summary returned all zeros right after a restart despite 900 real
traces). That was fixed with a real asyncio.Lock.

They now read this box's own logs (agent/metrics.py): a file scan measured in
milliseconds, run in a thread, with no cache, no lock and no pre-warm. A cold
call returns real numbers on the first request -- which is what this test
pins, so nobody reintroduces a cache without a reason.
"""

import agent.server as srv
from agent import metrics


def test_the_analytics_endpoints_hold_no_cache_state():
    for gone in ("_model_usage_cache", "_tool_reliability_cache", "_trace_summary_cache",
                 "_refresh_model_usage", "_refresh_tool_reliability", "_refresh_trace_summary",
                 "_scan_langsmith_model_usage", "_scan_langsmith_tool_reliability",
                 "_scan_langsmith_trace_summary"):
        assert not hasattr(srv, gone), (
            f"{gone} is back: if a cache is needed again, so is the lock that made "
            "a concurrent cold caller wait instead of reading an empty one")


def test_a_cold_call_computes_rather_than_returning_empty(tmp_path, monkeypatch):
    """The failure mode the lock existed for -- an empty first answer -- is
    impossible when the first answer is computed from a local file."""
    import json
    import time

    log = tmp_path / "routing.jsonl"
    log.write_text(json.dumps({
        "ts": time.time(), "call_id": "a", "routed_model": "agent-coder",
        "requested_model": "deepseek/deepseek-v4.1-flash", "prompt_tokens": 100,
        "completion_tokens": 10, "cost": 0.01, "duration_s": 2.0, "task_id": "T",
    }) + "\n")
    monkeypatch.setattr(metrics, "ROUTING_LOG", log)

    assert metrics.model_usage()["models"][0]["calls"] == 1
    assert metrics.run_summary()["trace_count"] == 1
