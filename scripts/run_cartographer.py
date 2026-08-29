"""Entry point for scheduled codebase mapping -- the companion to
scripts/run_consolidation.py.

    .venv/bin/python scripts/run_cartographer.py [--force] [repo ...]

Cheap to run often: each project's inventory is hashed, and the model is only
called when a repo's structure actually changed. --force rebuilds regardless.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cartographer import run_cartographer
from agent.config import PROJECTS, load_config
from agent.graph import open_store


async def main(repos: list[str], force: bool) -> int:
    """Returns the number of projects that failed, for the exit code."""
    config = load_config()
    failures: list[tuple[str, str]] = []
    async with open_store(config) as store:
        for repo in repos:
            print(f"[cartographer] {repo}: running...", flush=True)
            try:
                summary = await run_cartographer(config, repo, store, force=force)
            except Exception as e:  # noqa: BLE001 -- one project must not abort the rest
                failures.append((repo, str(e)))
                print(f"[cartographer] {repo}: FAILED -- {e}", flush=True)
                continue
            print(f"[cartographer] {repo}: {summary}", flush=True)

    # Same loud-failure contract as consolidation: a silent exit 0 on a broken
    # run is how the nightly consolidation stayed dead for months.
    if failures:
        print("", flush=True)
        print("=" * 72, flush=True)
        print(f"CARTOGRAPHY FAILED for {len(failures)} of {len(repos)} project(s):", flush=True)
        for repo, err in failures:
            print(f"  - {repo}: {err[:400]}", flush=True)
        print("=" * 72, flush=True)
        print("The codebase-map skill is STALE for the projects above — agents will", flush=True)
        print("fall back to exploring those repos from scratch until this is fixed.", flush=True)
    return len(failures)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    repos = args or list(PROJECTS.keys())
    sys.exit(1 if asyncio.run(main(repos, force)) else 0)
