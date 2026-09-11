"""Who changed what, kept where it survives a restart.

This system was built for one operator, and for one operator the answer to
"who approved that?" is always the same person. The moment a second account
exists, it stops being: an admin flips another user's auto-approve, someone
sets a GitHub source to Auto, someone generates a deploy key, and a week
later the only record is a Telegram message in a chat that may have been
cleared -- if notifications were even switched on.

Telegram is not an audit log. It is a notification channel: best-effort,
unordered, per-recipient, and deleted at the whim of whoever owns the chat.
This is the log: append-only in the same Postgres store that holds tasks and
memory, so it rides along in the backup and outlives every restart.

What goes in here is deliberately narrow. Not "everything that happened" --
every task and every model call is already recorded elsewhere -- but the
decisions that *move a control*: who was granted access to skip a gate, who
onboarded a project, who approved a specific gated command, who let an inbox
source create tasks on its own, who minted a key that can push.

Writing a record never blocks the thing being recorded. An approval that
fails because the log write failed would make the log a new way to break the
system; instead a failure is logged at ERROR and the action proceeds. The
trade is deliberate and worth knowing about when reading this page as
evidence: it is a very good record of what happened, not a court exhibit.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = ("audit",)

# How many records the store keeps. Each is a few hundred bytes, so this is
# small; the cap exists so an automated toggler cannot grow the table without
# bound, not to save space.
MAX_RECORDS = 2000

# Trimming reads the whole namespace, so it is not done on every write.
_TRIM_EVERY = 100
_writes_since_trim = 0

# The actions worth a record, with the sentence the dashboard shows. Adding
# one here and forgetting the call site is the obvious failure, so
# tests/test_audit.py asserts every action in this table is actually recorded
# somewhere in agent/.
ACTIONS: dict[str, str] = {
    "project.onboard": "onboarded a project",
    "command.approve": "approved a gated command",
    "command.reject": "rejected a gated command",
    "merge.approve": "approved a merge",
    "merge.request_changes": "sent a merge back for changes",
    "settings.auto_approve": "changed auto-approve of commands",
    "settings.merge_review": "changed the final merge review",
    "settings.auto_approve_repos": "changed which projects auto-approve covers",
    "github.source_policy": "changed a GitHub inbox source",
    "deploy_key.generate": "generated a deploy key",
    "deploy_key.delete": "deleted a deploy key",
}


def _key(ts: float) -> str:
    """Sortable by time, unique under concurrency. Zero-padded so string
    order is time order -- the store has no ORDER BY for us to use."""
    return f"{int(ts * 1000):015d}-{uuid.uuid4().hex[:8]}"


async def record(store, *, actor: str, action: str, target: str | None = None,
                 detail: str | None = None, extra: dict[str, Any] | None = None) -> dict | None:
    """Append one record. Returns it, or None if it could not be written.

    `actor` is the account that made the decision -- an email, or a name like
    "github-inbox" for something the system did on a policy the operator set
    earlier. Never a session token, never an IP: this page is read by people
    and shown in a dashboard.
    """
    if action not in ACTIONS:
        # A typo'd action would be invisible in the UI (no label) and
        # unsearchable. Better to notice in the log than to store it.
        logger.error("audit: unknown action %r -- not recorded", action)
        return None
    if store is None:
        return None
    ts = time.time()
    entry = {
        "ts": ts,
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail,
        **(extra or {}),
    }
    try:
        await store.aput(NAMESPACE, _key(ts), entry)
    except Exception as e:  # noqa: BLE001 -- never block the audited action
        logger.error("audit: could not record %s by %s: %s", action, actor, e)
        return None
    await _maybe_trim(store)
    return entry


async def _maybe_trim(store) -> None:
    global _writes_since_trim
    _writes_since_trim += 1
    if _writes_since_trim < _TRIM_EVERY:
        return
    _writes_since_trim = 0
    try:
        items = await store.asearch(NAMESPACE, limit=MAX_RECORDS * 2)
        if len(items) <= MAX_RECORDS:
            return
        # Oldest first, drop the overflow.
        for item in sorted(items, key=lambda i: i.value.get("ts", 0))[:len(items) - MAX_RECORDS]:
            await store.adelete(NAMESPACE, item.key)
    except Exception as e:  # noqa: BLE001
        logger.warning("audit: trim failed: %s", e)


async def recent(store, limit: int = 100) -> list[dict]:
    """Newest first. Anything unreadable is skipped rather than raising: a
    single malformed row must not blank the whole page."""
    if store is None:
        return []
    try:
        items = await store.asearch(NAMESPACE, limit=max(limit, 1) * 2)
    except Exception as e:  # noqa: BLE001
        logger.warning("audit: could not read the log: %s", e)
        return []
    rows = []
    for item in items:
        value = item.value if isinstance(item.value, dict) else None
        if not value or "action" not in value:
            continue
        rows.append({**value, "label": ACTIONS.get(value["action"], value["action"])})
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows[:limit]
