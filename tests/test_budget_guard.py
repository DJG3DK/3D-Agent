"""Real unit tests for BudgetTracker/BudgetGuardMiddleware's cost math and
ceiling enforcement -- the one non-negotiable hard $ requirement in this
whole system. Written during the full LangGraph/deepagents docs audit,
which found zero automated test coverage of this logic anywhere; all prior
validation was live spike-testing against real infra (real, but not
repeatable/regression-safe). Uses fakes, not mocking frameworks -- these
are pure objects with the exact attributes BudgetGuardMiddleware reads.
"""

from langchain_core.messages import AIMessage
from langchain.agents.middleware.types import ModelResponse

from agent.middleware.budget_guard import BudgetExceededError, BudgetGuardMiddleware, BudgetTracker


def _msg(response_metadata: dict, usage_metadata: dict | None = None) -> AIMessage:
    m = AIMessage(content="hi", response_metadata=response_metadata)
    if usage_metadata is not None:
        m.usage_metadata = usage_metadata
    return m


async def _handler_returning(*messages) -> ModelResponse:
    return ModelResponse(result=list(messages))


def test_tracker_starts_at_zero_by_default():
    tracker = BudgetTracker(budget_usd=1.0)
    assert tracker.total_cost == 0.0
    assert tracker.budget_usd == 1.0


def test_tracker_seeds_starting_cost_for_resume():
    tracker = BudgetTracker(budget_usd=5.0, starting_cost=2.5)
    assert tracker.total_cost == 2.5


async def test_prefers_litellm_reported_cost_when_present():
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    msg = _msg({"token_usage": {"cost": 0.0123}})

    response = await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))

    assert response.result == [msg]
    assert tracker.total_cost == 0.0123


async def test_falls_back_to_estimate_cost_when_litellm_cost_absent(monkeypatch):
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    # No "cost" key in token_usage (or no token_usage at all) -- the real
    # shape once work.py moved to astream_events(version="v3") streaming,
    # where LiteLLM's cost annotation doesn't survive.
    msg = _msg(
        {"model_name": "z-ai/glm-5.2"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 200},
    )

    def fake_estimate_cost(model_name, input_tokens, output_tokens, cache_read_tokens=0):
        assert model_name == "z-ai/glm-5.2"
        assert input_tokens == 1000
        assert output_tokens == 200
        assert cache_read_tokens == 0  # no input_token_details on this message
        return 0.005

    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", fake_estimate_cost)

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))

    assert tracker.total_cost == 0.005


async def test_passes_cache_read_tokens_through_to_estimate_cost(monkeypatch):
    """usage_metadata.input_token_details.cache_read must reach
    estimate_cost -- this is what makes a long, repeated-context tool-calling
    conversation get priced at the real discounted cache rate instead of
    full input price for every resent token (see model_rates.py's own
    docstring on why that distinction matters)."""
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    msg = _msg(
        {"model_name": "z-ai/glm-5.3"},
        usage_metadata={
            "input_tokens": 118294,
            "output_tokens": 376,
            "input_token_details": {"cache_read": 117888},
        },
    )

    def fake_estimate_cost(model_name, input_tokens, output_tokens, cache_read_tokens=0):
        assert cache_read_tokens == 117888
        return 0.0329

    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", fake_estimate_cost)

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))

    assert tracker.total_cost == 0.0329


async def test_sums_cost_across_multiple_messages_in_one_response():
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    msg1 = _msg({"token_usage": {"cost": 0.01}})
    msg2 = _msg({"token_usage": {"cost": 0.02}})

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg1, msg2))

    assert tracker.total_cost == 0.03


async def test_trips_after_a_call_that_crosses_the_ceiling():
    tracker = BudgetTracker(budget_usd=0.05, starting_cost=0.04)
    mw = BudgetGuardMiddleware(tracker)
    msg = _msg({"token_usage": {"cost": 0.02}})  # 0.04 + 0.02 = 0.06 >= 0.05

    try:
        await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))
        raised = False
    except BudgetExceededError as e:
        raised = True
        assert e.spent == 0.06
        assert e.budget == 0.05

    assert raised, "expected BudgetExceededError once the ceiling is crossed"
    # The cost of the call that crossed the ceiling is still recorded --
    # the tracker reflects real spend even on the trip, not a rollback.
    assert tracker.total_cost == 0.06


async def test_refuses_before_spending_when_already_over_budget():
    tracker = BudgetTracker(budget_usd=1.0, starting_cost=1.5)  # already over
    mw = BudgetGuardMiddleware(tracker)
    handler_called = False

    async def handler(req):
        nonlocal handler_called
        handler_called = True
        return ModelResponse(result=[])

    try:
        await mw.awrap_model_call(request=None, handler=handler)
        raised = False
    except BudgetExceededError:
        raised = True

    assert raised
    # The whole point: refuse BEFORE the call, not after -- no new spend
    # should ever be incurred once already over budget.
    assert handler_called is False


async def test_shared_tracker_aggregates_across_coordinator_and_subagent():
    """The design this whole class exists for: coordinator and every
    subagent must share ONE BudgetTracker instance so the ceiling is
    enforced against real AGGREGATE spend, not budget_usd per middleware
    instance. See budget_guard.py's own module docstring.
    """
    tracker = BudgetTracker(budget_usd=0.10)
    coordinator_mw = BudgetGuardMiddleware(tracker)
    subagent_mw = BudgetGuardMiddleware(tracker)

    await coordinator_mw.awrap_model_call(
        request=None, handler=lambda req: _handler_returning(_msg({"token_usage": {"cost": 0.06}}))
    )
    assert tracker.total_cost == 0.06

    try:
        await subagent_mw.awrap_model_call(
            request=None, handler=lambda req: _handler_returning(_msg({"token_usage": {"cost": 0.06}}))
        )
        raised = False
    except BudgetExceededError:
        raised = True

    # 0.06 + 0.06 = 0.12 >= 0.10 -- the SUBAGENT's own call trips the SAME
    # shared ceiling, proving aggregation, not independent per-instance budgets.
    assert raised
    assert tracker.total_cost == 0.12


def test_estimate_cost_strict_raises_on_unpriced_model():
    """audit C-1: for a hard ceiling, 'unknown price' must not read as $0.
    estimate_cost stays lenient (analytics); estimate_cost_strict raises so the
    budget guard fails safe."""
    from agent.tools.model_rates import (
        UnpricedModelError,
        estimate_cost,
        estimate_cost_strict,
    )
    import agent.tools.model_rates as mr
    saved = mr._rates  # restore -- this global is shared across the whole suite
    try:
        mr._rates = {"known/model": {"input": 1e-6, "output": 2e-6, "cache_read": 1e-7}}
        assert estimate_cost("who/knows", 1000, 1000) == 0.0        # lenient: unknown -> 0
        import pytest
        with pytest.raises(UnpricedModelError):                     # strict: unknown -> raise
            estimate_cost_strict("who/knows", 1000, 1000)
        assert estimate_cost_strict("known/model", 1000, 1000) > 0  # strict: known -> real
    finally:
        mr._rates = saved
