"""BudgetGuardMiddleware — the primary, code-enforced hard $ ceiling.

Enforces the budget with a check after every individual model call, inside
the model-call itself — the tightest enforcement point deepagents exposes
(`awrap_model_call`), a Python exception propagating out of the call rather
than a text instruction the model can ignore. This is the primary defense;
the outer `work` node's own watchdog over the whole astream() is a secondary
backstop, not a substitute.

Must be attached to the coordinator's own `middleware=[...]` and to every
subagent's own `middleware=[...]` list individually -- a SubAgent spec's
`middleware` list is merged in by name (replace-if-name-matches, else
append), never inherited by default. This does not mean subagents otherwise
run with an empty middleware stack -- deepagents auto-prepends its own fresh
FilesystemMiddleware/SubAgentMiddleware/summarization/PatchToolCallsMiddleware/
prompt-caching to every subagent independently of anything in `spec[
"middleware"]`. What's true, and the actual reason this class exists, is
narrower: our custom middleware specifically (this one) is never
auto-attached to a subagent just because it's on the coordinator -- that one
has to be listed explicitly per subagent, or that subagent spends
completely unmetered against the shared budget.

Cost is read from LiteLLM's own computed `response_metadata["token_usage"]
["cost"]` when present -- the exact, authoritative cost LiteLLM itself
billed. This is never present once a model call goes through
`agent.astream_events(..., version="v3")`: that API always registers a
`.messages` native projection, which forces every model call into a real
token stream rather than a single non-streaming response, and LiteLLM's
extra cost annotation does not survive OpenAI-compatible SSE streaming no
matter what `stream_usage`/`stream_options` are set (the separate, standard
`usage_metadata` field -- input/output token counts -- comes through fine
with `stream_usage=True` on `llm_for_role()`). So this falls back to
`agent/tools/model_rates.estimate_cost()`, computed from `usage_metadata`'s
real token counts against the LLM router's own config for whichever
underlying model `response_metadata["model_name"]` names -- not a rate
table that could silently drift, since it's parsed from the exact same
config file the proxy itself uses for billing. Falls back to 0.0 (not a
guess) only if the model is unrecognized in that file entirely.

Shared tracker, not per-instance state: a BudgetGuardMiddleware that held
its own running total would let the ceiling be blown well past its intended
value, since coordinator and every subagent each get their own middleware
instance (see above) -- if each tracked independently, the coordinator could
spend up to budget_usd and each subagent could also independently spend up
to budget_usd, for a true aggregate of budget_usd * (1 + subagent_count),
not budget_usd. `BudgetTracker` is the one shared mutable object every
middleware instance for a given task invocation must wrap, so the ceiling is
enforced against the real aggregate spend across the coordinator and every
subagent call combined. Safe without locking because subagents in this
system run synchronously (the coordinator blocks on each `task()`
delegation) -- only one model call for the whole task is ever in flight at
a time.
"""

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.callbacks import AsyncCallbackHandler

from agent.tools.model_rates import UnpricedModelError, estimate_cost_strict


class BudgetExceededError(Exception):
    def __init__(self, spent: float, budget: float):
        super().__init__(f"budget exceeded: ${spent:.4f} spent against a ${budget:.2f} ceiling")
        self.spent = spent
        self.budget = budget


class BudgetTracker:
    """One instance per task invocation, shared by reference across the
    coordinator's and every subagent's own BudgetGuardMiddleware. Never
    share an instance across different tasks/threads — `build_deep_agent`
    constructs a fresh one per `work`-node invocation, seeded with whatever
    `cost_so_far` the outer AgentState already carries in from a resume, so
    a resumed task keeps counting from its real total rather than
    restarting at zero.
    """

    def __init__(self, budget_usd: float, starting_cost: float = 0.0):
        self.budget_usd = budget_usd
        self.total_cost = starting_cost


class BudgetGuardMiddleware(AgentMiddleware):
    def __init__(self, tracker: BudgetTracker):
        super().__init__()
        self.tracker = tracker

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        if self.tracker.total_cost >= self.tracker.budget_usd:
            # Refuse before spending on a call that's already over budget —
            # don't wait for this call's own cost to land before tripping.
            raise BudgetExceededError(self.tracker.total_cost, self.tracker.budget_usd)

        response = await handler(request)

        for msg in response.result:
            cost = 0.0
            meta = getattr(msg, "response_metadata", None) or {}
            token_usage = meta.get("token_usage") or {}
            if "cost" in token_usage:
                cost = float(token_usage["cost"])
            else:
                usage = getattr(msg, "usage_metadata", None) or {}
                cache_read = (usage.get("input_token_details") or {}).get("cache_read", 0)
                try:
                    cost = estimate_cost_strict(
                        meta.get("model_name"),
                        usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0),
                        cache_read_tokens=cache_read,
                    )
                except UnpricedModelError as e:
                    # An unpriced model must not read as $0 against a hard
                    # ceiling (audit C-1). Charge a conservative non-zero
                    # placeholder so the ceiling still advances and log loudly
                    # -- undercounting to zero is the exact failure that let
                    # 1.4M tokens through a $5 cap. The real fix is adding the
                    # model's rate; this makes the gap self-announcing and
                    # bounded rather than silent and unbounded.
                    import logging as _logging
                    _logging.getLogger("3d-agent").warning(
                        "budget: %s -- charging $0.02/1k output tokens as a placeholder", e
                    )
                    cost = usage.get("output_tokens", 0) / 1000 * 0.02
            self.tracker.total_cost += cost

        if self.tracker.total_cost >= self.tracker.budget_usd:
            raise BudgetExceededError(self.tracker.total_cost, self.tracker.budget_usd)

        return response


class BudgetMeterCallback(AsyncCallbackHandler):
    """Meters model calls that never pass through `awrap_model_call`.

    SummarizationMiddleware invokes its own summary model directly
    (`self._summary_model.ainvoke(...)` inside `_acreate_summary`) -- that call
    is not a graph model node, so no agent middleware wraps it and its spend
    was invisible to the tracker: measured 2026-08-27 at $0.48 of $16.43 over
    48h (2.9% of all router spend) never counted against any ceiling.

    A LangChain callback fires on every call made BY the model object it is
    attached to, regardless of who invokes it or how -- which makes attaching
    this to the summarizer model itself (see `llm_for_role(callbacks=...)`)
    the one seam that catches the direct-ainvoke path.

    Meter only, never a gate: `on_llm_end` cannot usefully refuse a call that
    already completed, and raising from inside a callback would surface as a
    summarization failure -- which SummarizationMiddleware handles by
    substituting fallback text for the ENTIRE prior conversation (the exact
    silent-history-wipe failure documented at SUMMARIZATION_TRIM_TOKENS).
    The BudgetGuardMiddleware on the next real model call trips the ceiling
    with this spend already counted, one summarizer call later at most.

    Token counts are read from generation_info/usage_metadata the same way
    BudgetGuardMiddleware reads them, and priced through the same
    estimate_cost_strict against the router's own config -- one pricing
    source, not two that can drift. The unpriced-model fallback mirrors the
    guard's (audit C-1): a conservative non-zero placeholder, never $0.
    """

    def __init__(self, tracker: BudgetTracker):
        super().__init__()
        self.tracker = tracker

    async def on_llm_end(self, response, **kwargs) -> None:
        for gens in response.generations:
            for gen in gens:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                usage = getattr(msg, "usage_metadata", None) or {}
                if not usage:
                    continue
                meta = getattr(msg, "response_metadata", None) or {}
                token_usage = meta.get("token_usage") or {}
                if "cost" in token_usage:
                    self.tracker.total_cost += float(token_usage["cost"])
                    continue
                cache_read = (usage.get("input_token_details") or {}).get("cache_read", 0)
                try:
                    self.tracker.total_cost += estimate_cost_strict(
                        meta.get("model_name"),
                        usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0),
                        cache_read_tokens=cache_read,
                    )
                except UnpricedModelError as e:
                    import logging as _logging
                    _logging.getLogger("3d-agent").warning(
                        "budget meter: %s -- charging $0.02/1k output tokens as a placeholder", e
                    )
                    self.tracker.total_cost += usage.get("output_tokens", 0) / 1000 * 0.02
