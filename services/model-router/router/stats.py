"""A live read of the ledger, per alias.

The router writes every call down already; this turns that into the answer to
the questions actually asked of it at 2am -- which role is slow right now,
which provider is serving it, what has it cost, and is anything failing.

It exists because the one thing nobody could see was throughput. A coder call
that ran 1802 seconds and returned 280 tokens looked exactly like a healthy
1217-second call that returned 64,904, and there was no view that separated
them. `tokens_per_s` does.

Reads the tail of the file rather than the whole thing: the ledger is capped at
50MB and a stats call must not become the most expensive thing the process
does.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

# Enough to cover a busy hour without walking a 50MB file.
TAIL_BYTES = 4_000_000


def _tail_rows(path: Path, since: float) -> list[dict]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()          # discard the partial first line
            data = f.read()
    except OSError:
        return []
    rows = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue                  # a torn line: the file is being appended to
        if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)) and row["ts"] >= since:
            rows.append(row)
    return rows


def summarise(path: Path, window_s: float, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    rows = _tail_rows(path, now - window_s)
    by_alias: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_alias[r.get("alias") or "unknown"].append(r)

    out = []
    for alias, calls in by_alias.items():
        errors = [c for c in calls if c.get("error")]
        ok = [c for c in calls if not c.get("error")]
        durations = sorted(float(c["duration_s"]) for c in ok if c.get("duration_s"))
        out_tokens = sum(int(c.get("completion_tokens") or 0) for c in ok)
        total_time = sum(durations)
        prompt = sum(int(c.get("prompt_tokens") or 0) for c in ok)
        cached = sum(int(c.get("cached_tokens") or 0) for c in ok)
        out.append({
            "alias": alias,
            "calls": len(calls),
            "errors": len(errors),
            "error_rate": round(len(errors) / len(calls), 4) if calls else 0.0,
            "p50_s": round(statistics.median(durations), 2) if durations else None,
            "p95_s": round(durations[min(int(len(durations) * 0.95), len(durations) - 1)], 2) if durations else None,
            "max_s": round(durations[-1], 2) if durations else None,
            # The number that separates "stuck" from "busy". A long call
            # producing tokens steadily is working; a long call producing
            # almost none is not, and duration alone cannot tell them apart.
            "tokens_per_s": round(out_tokens / total_time, 1) if total_time else None,
            "cost_usd": round(sum(float(c.get("cost") or 0) for c in calls), 4),
            "cache_hit_rate": round(cached / prompt, 3) if prompt else None,
            "retries": sum(1 for c in calls if (c.get("attempt") or 1) > 1),
            "providers": sorted({c.get("provider") for c in ok if c.get("provider")}),
            "models": sorted({c.get("requested_model") for c in ok if c.get("requested_model")}),
        })
    out.sort(key=lambda a: a["calls"], reverse=True)
    return {
        "window_s": window_s,
        "calls": len(rows),
        "cost_usd": round(sum(float(r.get("cost") or 0) for r in rows), 4),
        "aliases": out,
    }
