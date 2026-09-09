"""The planning brief: written first, pinned for the rest of the turn.

Why this exists
---------------
A planning turn on 2026-09-08 spent 68 model calls and twenty minutes reading
the target repo before writing a word of plan. Its context grew from 8k to
169k tokens, SummarizationMiddleware compacted it, and the operator's request
-- the OLDEST content in the window -- was the first thing compressed. The
system prompt's plea not to "lose track of what the user actually asked for"
is exactly the kind of instruction a model under compaction cannot honour:
the request is no longer in front of it.

Two middlewares fix that structurally rather than hortatively:

BriefFirstMiddleware
    Until the session has a brief, the model sees ONE tool: save_brief (plus
    describe_image, so an attached screenshot can inform the brief). It
    cannot read a file before it has written down what it is reading FOR.
    Same mechanism HiddenToolsMiddleware uses -- a tool absent from the
    request schema is a tool the model does not call -- and the same
    justification: a prompt instruction is advisory, the schema is not.

PinnedBriefMiddleware
    Appends the current brief to the system prompt on every model call.
    SummarizationMiddleware compacts `messages`; it never touches the system
    message, so the brief is the one piece of the request that survives any
    number of compactions verbatim. The same block carries the skills
    save_brief matched against the request, so "read the architecture skill
    for the thing you are planning" is in front of the model on every call,
    not only in the tool result it saw forty calls ago.

Both share `brief_ref`, the mutable dict make_planning_tools' save_brief
writes into (the plan_ref pattern: a tool closure is the only thing that sees
the call, so the result comes back by reference). The server persists the
brief in the session's Store meta and seeds it back on the next turn, so a
follow-up message does not force a fresh brief -- the pinned block says to
update it if the request changed.
"""

from langchain_core.messages import SystemMessage
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

# Tools the model may use before it has written a brief.
BRIEF_FIRST_ALLOWED = frozenset({"save_brief", "describe_image"})


def render_brief(brief: dict | None) -> str:
    """The pinned block. Deterministic, so a test can assert on it and a
    reader of the raw prompt can find it."""
    if not brief:
        return ""
    lines = [
        "=== PINNED BRIEF (the operator's request, restated by you; survives compaction) ===",
        f"GOAL: {brief.get('goal', '').strip()}",
        f"DELIVERABLE: {brief.get('deliverable', '').strip()}",
    ]
    if brief.get("out_of_scope"):
        lines.append(f"OUT OF SCOPE: {brief['out_of_scope'].strip()}")
    if brief.get("needs"):
        lines.append(f"EXPECTED TO NEED: {brief['needs'].strip()}")
    skills = brief.get("matched_skills") or []
    if skills:
        lines.append("READ THESE SKILLS BEFORE ANY REPO FILE (matched to this request):")
        lines.extend(f"  - /skills/{name}/SKILL.md" for name in skills)
    lines.append(
        "Every read and every paragraph of the plan must serve the GOAL above. If the operator's "
        "latest message changes the request, call save_brief again before anything else."
    )
    lines.append("=== END BRIEF ===")
    return "\n".join(lines)


def _tool_name(t) -> str:
    if isinstance(t, dict):
        return t.get("name") or (t.get("function") or {}).get("name") or ""
    return getattr(t, "name", "")


class BriefFirstMiddleware(AgentMiddleware):
    """Hides every tool but save_brief (and describe_image) until a brief exists."""

    def __init__(self, brief_ref: dict, allowed: frozenset[str] = BRIEF_FIRST_ALLOWED):
        super().__init__()
        self.brief_ref = brief_ref
        self.allowed = allowed

    def _narrow(self, request: ModelRequest) -> ModelRequest:
        if self.brief_ref.get("brief"):
            return request
        return request.override(tools=[t for t in request.tools if _tool_name(t) in self.allowed])

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return handler(self._narrow(request))

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return await handler(self._narrow(request))


class PinnedBriefMiddleware(AgentMiddleware):
    """Appends render_brief(brief_ref["brief"]) to the system message of every call."""

    def __init__(self, brief_ref: dict):
        super().__init__()
        self.brief_ref = brief_ref

    def _pin(self, request: ModelRequest) -> ModelRequest:
        block = render_brief(self.brief_ref.get("brief"))
        if not block:
            return request
        base = request.system_message.text if request.system_message is not None else ""
        return request.override(system_message=SystemMessage(content=f"{base}\n\n{block}" if base else block))

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return handler(self._pin(request))

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return await handler(self._pin(request))
