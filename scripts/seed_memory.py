"""One-time (idempotent) seeding of memory content for every configured project.
Safe to re-run -- seed_memory/seed_org_memory never overwrite existing content.

Reads memory/org.md for org-wide conventions and memory/<project>.md for each
project in PROJECTS. Falls back to the *.example.md templates in the same
directory if a real file isn't present, so this script also runs cleanly
against the example project configuration.

    .venv/bin/python scripts/seed_memory.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import PROJECTS, load_config
from agent.deep_agent import seed_memory, seed_org_memory
from agent.graph import open_checkpointer, open_store

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"


def _read_memory(name: str, fallback: str) -> str:
    real = MEMORY_DIR / f"{name}.md"
    example = MEMORY_DIR / f"{fallback}.example.md"
    path = real if real.exists() else example
    return path.read_text()


async def main() -> None:
    config = load_config()
    async with open_checkpointer(config), open_store(config) as store:
        await seed_org_memory(store, _read_memory("org", "org"))
        print("seeded org-memory")
        for project in PROJECTS:
            content = _read_memory(project, "example-project")
            await seed_memory(project, store, content)
            print(f"seeded {project} memory")


if __name__ == "__main__":
    asyncio.run(main())
