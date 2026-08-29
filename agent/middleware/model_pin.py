"""PlanCodeModelMiddleware — deterministic two-model split for the coordinator.

One pinned model per role rather than an adaptive pool. The coordinator's
work has two distinct shapes, and each is pinned to a different model:

  - PLANNING: the first model turn of an inner thread — the turn that reads
    the goal/context and writes the todo plan. Pinned to a strong general
    reasoner (agent-planner).
  - CODING: every turn after that — tool-calling, editing, running checks.
    Pinned to a coding specialist (agent-coder).

The split is deterministic, not classified: a turn is a PLANNING turn iff the
request's message history contains no AIMessage yet. That is exactly the
first turn of a fresh thread — including a generation-bumped fresh thread
after a poisoned-context restart (work.py's inner_thread_generation), where
re-planning is precisely what's wanted. Everything downstream of the first
AI response (loop-back feedback, approvals, operator nudges) is CODING: the
plan exists, the remaining work is executing it.

Coordinator-only — subagents have their own single pinned models.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse


def is_planning_turn(messages) -> bool:
    """True iff this turn responds directly to fresh OUTER input.

    An earlier version of this rule was "no AIMessage in history yet" --
    literally only the first turn of a thread. That meant a reviewer
    NEEDS_FIXES round, a checks-failed loopback, or an operator resume
    message -- all genuine re-planning moments -- were handled by the coder
    pin instead of the planner.

    Current rule: a turn is a planning turn iff the LAST message is a
    HumanMessage. work_node only ever injects HumanMessages at the seams
    (the goal, verify_and_ship feedback, operator messages), so "last
    message is human" exactly marks the first response to each new piece of
    outer input -- the planner reads the feedback and decides the approach.
    From the model's first tool call onward the last message is an AI/Tool
    message, so execution turns stay on the coder. Summarization inserts
    its summary mid-list, never last, so it can't fake a planning turn.
    """
    msgs = [m for m in (messages or []) if not isinstance(m, SystemMessage)]
    if not msgs:
        return True
    # Scan the suffix after the model's last own message: if any human input
    # arrived since the model last spoke, this turn responds to it.
    # "Last message is human" alone misses a real case: resuming a thread
    # stopped mid-tool-loop appends the operator's message and then the
    # interrupted tool calls' results, so the human input sits one or more
    # slots before the end.
    for m in reversed(msgs):
        if isinstance(m, HumanMessage):
            return True
        if isinstance(m, AIMessage):
            return False
    return True   # no AI message at all yet -- first turn of the thread


class PlanCodeModelMiddleware(AgentMiddleware):
    def __init__(self, planner_model, coder_model):
        super().__init__()
        self.planner_model = planner_model
        self.coder_model = coder_model

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        model = self.planner_model if is_planning_turn(request.messages) else self.coder_model
        return await handler(request.override(model=model))
