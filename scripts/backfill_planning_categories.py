"""One-time backfill: classifies any existing planning session whose Store
meta predates category classification (no "category" key, or one that's
None) into the same fixed taxonomy a build task gets at creation -- lets the
sidebar group pre-existing planning sessions by category too, not just new
ones. Safe to re-run -- only ever touches a session missing a category, and
skips one with no title yet (nothing to classify -- it's never had a first
message).

    .venv/bin/python scripts/backfill_planning_categories.py [repo ...]

With no arguments, runs for every project in PROJECTS.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.classify import classify_task
from agent.config import PROJECTS, load_config
from agent.graph import open_checkpointer, open_store


async def main(repos: list[str]) -> None:
    config = load_config()
    async with open_checkpointer(config), open_store(config) as store:
        for repo in repos:
            items = await store.asearch(("planning", repo), limit=200)
            pending = [item for item in items if not item.value.get("category") and item.value.get("title")]
            if not pending:
                print(f"[backfill] {repo}: nothing to do ({len(items)} planning sessions)")
                continue
            print(f"[backfill] {repo}: classifying {len(pending)} of {len(items)} planning sessions...")
            for item in pending:
                title = item.value.get("title", "")
                classification = await classify_task(title, config)
                await store.aput(("planning", repo), item.key, {**item.value, "category": classification.category})
                print(f"[backfill] {repo}: {item.key} -> {classification.category} ({title[:60]!r})")


if __name__ == "__main__":
    repos = sys.argv[1:] or list(PROJECTS.keys())
    asyncio.run(main(repos))
