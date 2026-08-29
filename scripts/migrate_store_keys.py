"""One-time migration: move agent-visible store files from full-path keys to
route-stripped keys.

Background: app code (seeding, prompt reads) used full agent-visible paths as
raw store keys ('/memories/AGENTS.md'), while the agent's own file tools go
through the CompositeBackend, whose route stripping produces '/AGENTS.md'
(see agent/deep_agent.py's route_local_path). The two halves could not see
each other's writes -- agent-written memory was invisible to prompts, and
seeded skills were unreadable by the agent. App code now uses stripped keys
everywhere; this migrates any existing full-path data over.

Rules:
- Copy full-path value -> stripped key only if the stripped key is absent
  (an agent-written file at the stripped key is newer than the seed and is
  never clobbered).
- Delete the full-path key afterward either way, so stale copies can't be
  read by accident again.

Run: .venv/bin/python scripts/migrate_store_keys.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import PROJECTS, load_config
from agent.graph import open_store

# (namespace, route prefix) pairs whose keys may hold full-path entries.
NAMESPACES = (
    [(("org",), "/org-memory/")]
    + [((project,), "/memories/") for project in PROJECTS]
    + [(("skills", project), "/skills/") for project in PROJECTS]
)


async def main() -> None:
    config = load_config()
    async with open_store(config) as store:
        for namespace, route in NAMESPACES:
            items = await store.asearch(namespace, limit=100)
            for item in items:
                if not item.key.startswith(route):
                    continue  # already stripped (or something else entirely) -- leave it
                stripped = "/" + item.key[len(route):]
                existing = await store.aget(namespace, stripped)
                if existing is None:
                    await store.aput(namespace, stripped, item.value)
                    print(f"{namespace}: migrated {item.key!r} -> {stripped!r}")
                else:
                    print(f"{namespace}: kept newer {stripped!r}, dropping stale seed at {item.key!r}")
                await store.adelete(namespace, item.key)

        print("\n=== post-migration state ===")
        for namespace, _route in NAMESPACES:
            items = await store.asearch(namespace, limit=100)
            for item in items:
                print(f"{namespace}: {item.key!r} ({len(str(item.value))} chars)")


asyncio.run(main())
