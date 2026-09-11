"""Identity for stream log entries, so a reconnect cannot lose or duplicate one.

The task stream had two sources for the same log and no way to tell whether an
entry from one was the same entry from the other:

  * the live WebSocket, which carries entries as they happen;
  * the REST snapshot, whose `execution_log` is written by the graph only when
    a work pass RETURNS, so mid-pass it is an older, shorter list.

Both ends coped with "keep whichever list is longer" -- the server in
_fuller_log, the browser in its hydrate handler. Length is a proxy for
freshness and it is wrong in both directions: a snapshot that is longer but
older replaces newer live entries, and a snapshot that is shorter is thrown
away even when it holds entries the client never saw (everything before it
connected). Neither can merge, so a reconnect either shrinks the log or drops
history.

An entry gets a deterministic id here, computed from its own content, so the
same entry arriving twice by two routes is recognisably one entry. Content-
derived rather than a counter on purpose: the graph writes execution_log into
the checkpoint without going through this process at all, so there is nowhere
to hand out sequential ids that both paths would see. A hash needs no
coordination and survives a restart.

Events also get a monotonic `seq` per task, which is what lets the browser
open its socket BEFORE hydrating: frames that arrive during the hydrate are
buffered and replayed afterwards, and seq says what has already been applied.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any

# The fields that make an entry itself. Deliberately not the whole dict: a
# field added later (a UI hint, a flag) must not change the identity of an
# entry the browser already holds.
_IDENTITY_FIELDS = ("timestamp", "node", "step_id", "summary", "detail", "cost_usd")


def entry_id(entry: dict) -> str:
    """A stable short id for one log entry. Same content, same id, in this
    process or the next one."""
    if entry.get("id"):
        return str(entry["id"])
    material = json.dumps(
        {k: entry.get(k) for k in _IDENTITY_FIELDS}, sort_keys=True, default=str
    )
    return hashlib.blake2b(material.encode(), digest_size=8).hexdigest()


def stamp(entries: list | None) -> list:
    """Copies of `entries` with `id` set. Copies, not mutations: these dicts
    belong to the checkpoint and to the live buffer, and stamping must not
    write into either."""
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        out.append(entry if entry.get("id") else {**entry, "id": entry_id(entry)})
    return out


def merge(durable: list | None, buffered: list | None) -> list:
    """One log from the two sources, in order, with nothing lost or doubled.

    `durable` (the checkpoint, or whatever the caller considers authoritative)
    keeps its order and position; anything in `buffered` that the durable list
    does not already contain is appended, which is where it belongs -- the
    live buffer is what happened *since* the snapshot.

    Replaces "whichever list is longer", which could not do either half of
    that.
    """
    durable_stamped = stamp(durable)
    seen = {e["id"] for e in durable_stamped}
    out = list(durable_stamped)
    for entry in stamp(buffered):
        if entry["id"] not in seen:
            seen.add(entry["id"])
            out.append(entry)
    return out


class SeqCounter:
    """Monotonic event ids, per task, for the lifetime of this process.

    Per task rather than global so a busy neighbour cannot make one task's
    numbers jump; monotonic so the browser can drop a frame it has already
    applied when it replays the buffer it collected while hydrating.

    Not durable across a restart, and it does not need to be: the browser
    reconnects, hydrates, and starts again from whatever the new process
    hands it. The id orders frames within one connection's lifetime, which is
    the only place ordering can be ambiguous.
    """

    def __init__(self) -> None:
        self._counters: dict[str, Any] = {}

    def next(self, key: str) -> int:
        counter = self._counters.get(key)
        if counter is None:
            counter = self._counters[key] = itertools.count(1)
        return next(counter)

    def current(self, key: str) -> int:
        """The last id handed out for `key`, or 0. Used by the REST snapshot
        so the browser knows where the snapshot sits in the stream."""
        counter = self._counters.get(key)
        if counter is None:
            return 0
        # itertools.count has no peek; take one and put the value back by
        # rebuilding from it. Cheap, and this is called once per hydrate.
        value = next(counter)
        self._counters[key] = itertools.count(value)
        return value - 1

    def forget(self, key: str) -> None:
        self._counters.pop(key, None)
