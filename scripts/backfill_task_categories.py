"""One-time backfill: classifies any existing task whose Store meta predates
agent/classify.py (no "category" key at all) into the same fixed taxonomy
new tasks get at creation. Safe to re-run -- only ever touches tasks
missing the key, never reclassifies one that already has a category.

    .venv/bin/python scripts/backfill_task_categories.py [repo ...]

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
            items = await store.asearch(("tasks", repo), limit=200)
            pending = [item for item in items if "category" not in item.value]
            if not pending:
                print(f"[backfill] {repo}: nothing to do ({len(items)} tasks already classified)")
                continue
            print(f"[backfill] {repo}: classifying {len(pending)} of {len(items)} tasks...")
            for item in pending:
                goal = item.value.get("goal", "")
                classification = await classify_task(goal, config)
                await store.aput(("tasks", repo), item.key, {**item.value, "category": classification.category})
                print(f"[backfill] {repo}: {item.key} -> {classification.category} ({goal[:60]!r})")


if __name__ == "__main__":
    repos = sys.argv[1:] or list(PROJECTS.keys())
    asyncio.run(main(repos))
