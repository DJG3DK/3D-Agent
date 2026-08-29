"""Lets an operator send a message into a running task to nudge or redirect
it mid-flight. In-memory, per-process: fine for now since this runs as a
single pm2 fork, not a cluster -- same tradeoff server.py accepts for
_running_tasks/_subscribers.

Deliberately not a real LangGraph interrupt -- this doesn't pause execution
and wait; it's a mailbox that a node checks and drains at the start of its
next turn. Simpler to reason about, and advisory for the next step rather
than a hard synchronous blocker.
"""

_mailboxes: dict[str, list[str]] = {}


def add_message(task_id: str, text: str) -> None:
    _mailboxes.setdefault(task_id, []).append(text)


def pop_messages(task_id: str) -> list[str]:
    return _mailboxes.pop(task_id, [])
