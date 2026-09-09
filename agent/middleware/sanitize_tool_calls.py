"""SanitizeToolCallsMiddleware -- malformed tool calls never reach a provider.

Why this exists
---------------
2026-09-09: Kimi, mid-loop, emitted a write_todos call whose arguments were
truncated. LangChain keeps such a call as an `invalid_tool_call` with the raw
argument string, deepagents' PatchToolCallsMiddleware pairs it with a
"could not be executed" tool result, and both live in the thread from then
on. langchain-openai serialises an invalid call as a normal tool call with
the raw, non-JSON `function.arguments` -- which strict providers reject
outright: Alibaba answered every later planner turn with 400 "function.
arguments of the code model must be in JSON format", 37 times in one
afternoon. The planner alias fell back to deepseek each time, so the model
the operator pinned as planner silently never planned. Any provider that
validates history the same way would refuse the same conversation.

What it does: on every model call, the request's message list is rewritten
so that each malformed tool call and its placeholder result are removed,
and the assistant message that carried them gets a one-line note saying
what was dropped, so the model knows the action never happened. Valid tool
calls in the same message are kept. Only the REQUEST is rewritten -- the
checkpointed thread is left as is, so nothing about persistence or
hydration changes and the cleaning is idempotent per call.

"Malformed" means any of: LangChain's `invalid_tool_calls`; a tool call
whose args are not a JSON-serialisable dict; a raw `additional_kwargs`
tool call whose `function.arguments` is not valid JSON.
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import AIMessage, ToolMessage
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

logger = logging.getLogger("3d-agent")


def _json_ok(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _raw_args_ok(raw_call: dict) -> bool:
    args = ((raw_call or {}).get("function") or {}).get("arguments")
    if args is None or args == "":
        return True
    if not isinstance(args, str):
        return _json_ok(args)
    try:
        json.loads(args)
        return True
    except ValueError:
        return False


def _with_note(content, note: str):
    if isinstance(content, list):
        return [*content, {"type": "text", "text": note}]
    text = content or ""
    return f"{text}\n\n{note}" if text else note


def sanitize_messages(messages) -> tuple[list, int]:
    """(cleaned messages, number of malformed tool calls removed). Returns the
    original list object untouched when there is nothing to remove."""
    drop_ids: set = set()
    out: list = []
    removed = 0
    for m in messages:
        if isinstance(m, AIMessage):
            bad = list(m.invalid_tool_calls or [])
            good = []
            for tc in m.tool_calls or []:
                if isinstance(tc.get("args"), dict) and _json_ok(tc["args"]):
                    good.append(tc)
                else:
                    bad.append(tc)
            raw = (m.additional_kwargs or {}).get("tool_calls") or []
            raw_bad = [t for t in raw if not _raw_args_ok(t)]
            if not bad and not raw_bad:
                out.append(m)
                continue
            # One call can appear both as an invalid_tool_call and as a raw
            # additional_kwargs entry (LangChain parses the latter into the
            # former); count each id once.
            ids_here: set = set()
            for tc in bad:
                ids_here.add(tc.get("id") or f"anon:{id(tc)}")
            for t in raw_bad:
                ids_here.add(t.get("id") or f"anon:{id(t)}")
            drop_ids |= ids_here
            names = sorted({(tc.get("name") or ((tc.get("function") or {}).get("name")) or "unknown") for tc in [*bad, *raw_bad]})
            count = len(ids_here)
            removed += count
            note = (
                f"[{count} malformed tool call(s) removed from this message ({', '.join(names)}): the arguments "
                f"were not valid JSON, so the call(s) never ran. If that action is still needed, call the tool "
                f"again with well-formed arguments.]"
            )
            kwargs = {k: v for k, v in (m.additional_kwargs or {}).items() if k != "tool_calls"}
            out.append(AIMessage(
                content=_with_note(m.content, note),
                tool_calls=good,
                invalid_tool_calls=[],
                id=m.id,
                name=m.name,
                additional_kwargs=kwargs,
                response_metadata=dict(m.response_metadata or {}),
                usage_metadata=m.usage_metadata,
            ))
        elif isinstance(m, ToolMessage) and m.tool_call_id in drop_ids:
            continue
        else:
            out.append(m)
    return (out, removed) if removed else (messages, 0)


class SanitizeToolCallsMiddleware(AgentMiddleware):
    def _clean(self, request: ModelRequest) -> ModelRequest:
        cleaned, removed = sanitize_messages(request.messages)
        if not removed:
            return request
        logger.info("sanitize: removed %d malformed tool call(s) from the model request", removed)
        return request.override(messages=cleaned)

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return handler(self._clean(request))

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return await handler(self._clean(request))
