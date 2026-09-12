"""A plan whose counter cannot go backwards.

`write_todos` REPLACES the whole list -- that is the tool's contract -- and a
model asked to update its plan often complies by writing what is LEFT. Seen
live on 2026-09-12, task 01e640ef: twelve items with six completed became a
fresh six-item list with nothing completed, because the model wrote only the
remaining work. Nothing was lost and nothing had regressed; 23 files were
already changed and 967 lines deleted.

What the operator saw was the step strip falling from 6/12 to 0/6, twice, and
concluded the task was looping. That is the expensive part: a counter that
resets is indistinguishable from lost work, and the honest state of the run
was the opposite.

There is a second, worse consequence. `latest_todos` is not decoration -- the
commit gate reads it to decide whether the plan is finished, and
`incomplete_plan_streak` escalates a task whose plan never completes. A list
that keeps coming back all-pending can hold a finished task open.

So progress is merged rather than replaced: an item that was ever completed
stays completed, and one that disappears from a later list is kept (completed)
instead of vanishing. The model stays free to re-plan -- new items arrive,
reworded items arrive, the order is the model's -- but the record of what it
finished is the system's, not a thing each rewrite can erase.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")

COMPLETED = "completed"


def _key(todo: object) -> str | None:
    """Identity for matching one rewrite of the plan against the last.

    The content, whitespace-collapsed and case-folded. Not the index: a
    rewrite reorders freely. Not an id: `write_todos` has none to give.
    """
    if not isinstance(todo, dict):
        return None
    content = todo.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return _WS.sub(" ", content).strip().casefold()


def merge_todos(previous: list | None, incoming: list | None) -> list | None:
    """The list to show and to store, given what the model just wrote.

    * an incoming item that was completed before stays completed;
    * a completed item the rewrite dropped is kept, at the front, in its
      original order -- it is the work already done;
    * everything else is exactly what the model wrote, in its order.

    `incoming` of None means the model said nothing this turn, so the
    previous list stands.
    """
    if incoming is None:
        return previous
    if not isinstance(incoming, list):
        return incoming
    if not previous or not isinstance(previous, list):
        return list(incoming)

    done_before = [t for t in previous
                   if isinstance(t, dict) and t.get("status") == COMPLETED and _key(t)]
    done_keys = {_key(t) for t in done_before}
    incoming_keys = {_key(t) for t in incoming if _key(t)}

    out: list = []
    # Completed work the rewrite forgot. Kept so the denominator cannot
    # shrink below what was planned and finished.
    out.extend({**t, "status": COMPLETED} for t in done_before if _key(t) not in incoming_keys)
    for todo in incoming:
        key = _key(todo)
        if key and key in done_keys and isinstance(todo, dict):
            # Reappeared as pending after being finished: the status is the
            # one thing a rewrite does not get to undo.
            out.append({**todo, "status": COMPLETED})
        else:
            out.append(todo)
    return out


def counts(todos: list | None) -> tuple[int, int]:
    """(completed, total). What the step strip shows."""
    if not isinstance(todos, list):
        return (0, 0)
    items = [t for t in todos if isinstance(t, dict)]
    return (sum(1 for t in items if t.get("status") == COMPLETED), len(items))
