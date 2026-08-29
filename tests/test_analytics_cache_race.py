"""Regression test for a cold-start race in the LangSmith-backed analytics
caches (2026-08-24): _refresh_trace_summary (and its two siblings,
_refresh_model_usage/_refresh_tool_reliability) used a bare boolean
"refreshing" flag to avoid duplicate concurrent scans. That flag doesn't let
a second caller actually WAIT for the in-flight scan -- it just sees the
flag already set and returns immediately, leaving its own read of the cache
empty. This bites hardest right after a restart: the lifespan pre-warm task
kicks off a scan, and the very first real page load races it, sees "already
refreshing", and reads nothing back instead of the real data -- confirmed
live against the actual running server right after a restart, not just
reasoned about (GET /api/analytics/trace-summary came back all zeros with
cached: false immediately after a restart, despite 900 real traces existing).

A real asyncio.Lock fixes it: a second concurrent caller blocks on the lock
until the in-flight scan finishes, then finds the cache already fresh and
skips its own redundant scan -- but critically, it only reads the cache
AFTER that wait, never before.
"""

import asyncio
import time

import agent.server as srv


async def test_a_concurrent_caller_waits_for_the_in_flight_scan_instead_of_reading_an_empty_cache(monkeypatch):
    srv._trace_summary_cache["data"] = None
    srv._trace_summary_cache["at"] = 0.0

    def fake_scan():
        # Runs inside asyncio.to_thread -- a real (short) sleep here blocks
        # only that worker thread, giving the event loop a window to run
        # the second concurrent caller while the first is still "scanning".
        time.sleep(0.05)
        return {
            "trace_count": 900, "avg_latency_s": 56.5, "error_rate": 0.08,
            "total_input_tokens": 0, "total_output_tokens": 0,
        }

    monkeypatch.setattr(srv, "_scan_langsmith_trace_summary", fake_scan)

    results = []

    async def endpoint_like_call():
        # Mirrors get_trace_summary's own cold-cache branch: await the
        # refresh, then read whatever the cache holds right after.
        await srv._refresh_trace_summary()
        results.append(srv._trace_summary_cache["data"])

    await asyncio.gather(endpoint_like_call(), endpoint_like_call())

    assert all(r is not None for r in results), (
        "a concurrent caller read an empty cache instead of waiting for the in-flight scan"
    )
    assert all(r["trace_count"] == 900 for r in results)
