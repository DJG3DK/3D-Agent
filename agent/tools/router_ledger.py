"""The router's own per-call bill, read back into the budget.

BudgetGuardMiddleware has to price every model call itself, because LiteLLM's
exact cost annotation does not survive OpenAI-compatible streaming (see
budget_guard.py). An estimate computed from token counts and a rate table is
only as good as the table: on 2026-09-08 a planning turn was ended at
"$8.09 spent" when OpenRouter had billed $1.72 -- the pinned model id was
missing from OpenRouter's catalog, so its cache-read discount fell back to
the full input rate and 4.9M mostly-cached prompt tokens were charged 8x.

The router knows the true figure the moment each call completes: LiteLLM
asks OpenRouter for `usage.cost` on every request and hands it to
services/llm-router/custom_callbacks.py, which appends one line per call to
logs/routing.jsonl, keyed by the same `x-litellm-call-id` the proxy returned
in the response headers. This module reads that record back. The tracker
carries each call at its estimate until the router's line lands, then swaps
in the billed cost -- so the running total the ceiling is enforced against
is the router's own number for every call but the one that just finished.

Best-effort by design: the file lives beside the router, so an agent talking
to a router on another host (LLM_ROUTER_ROUTING_LOG unset and no local file)
simply never resolves anything and the estimate stands, exactly as before.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("3d-agent")

# Repo-relative, with an env override, like LLM_ROUTER_CONFIG_PATH in
# model_rates.py. This file is agent/tools/router_ledger.py, so the router
# lives three parents up.
ROUTING_LOG_PATH = Path(
    os.environ.get("LLM_ROUTER_ROUTING_LOG")
    or (Path(__file__).resolve().parents[2] / "services" / "llm-router" / "logs" / "routing.jsonl")
)

# routing.jsonl is appended forever and trimmed only past 5MB; a call we are
# waiting on is always within the last few hundred lines, so read the tail.
_TAIL_BYTES = 512_000


class RouterLedger:
    """Looks up the router's billed cost by call id. Re-reads the log only
    when its size or mtime changed, so polling it between model calls costs
    one stat() when nothing new has landed."""

    def __init__(self, path: Path | str = ROUTING_LOG_PATH):
        self.path = Path(path)
        self._signature: tuple[int, int] | None = None
        self._costs: dict[str, float] = {}

    def actual_costs(self, call_ids) -> dict[str, float]:
        """{call_id: billed_cost} for every requested id the router has
        logged so far. Ids the router has not billed yet are simply absent."""
        wanted = [c for c in call_ids if c]
        if not wanted:
            return {}
        self._refresh()
        return {c: self._costs[c] for c in wanted if c in self._costs}

    def _refresh(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            return  # no router log on this box: nothing ever resolves
        signature = (st.st_size, st.st_mtime_ns)
        if signature == self._signature:
            return
        try:
            with open(self.path, "rb") as f:
                if st.st_size > _TAIL_BYTES:
                    f.seek(st.st_size - _TAIL_BYTES)
                    f.readline()  # drop the partial line we landed inside
                data = f.read()
        except OSError as e:
            logger.debug("router ledger unreadable: %s", e)
            return
        costs: dict[str, float] = {}
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a line still being written, or a torn one
            call_id = row.get("call_id")
            cost = row.get("cost")
            if call_id and isinstance(cost, (int, float)):
                costs[call_id] = float(cost)
        self._costs = costs
        self._signature = signature
