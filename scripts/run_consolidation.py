"""Entry point for scheduled (cron) memory consolidation -- the
"background compute" half of the memory system, separate from any task's
own hot path. Run this periodically (e.g. daily) per project:

    .venv/bin/python scripts/run_consolidation.py [repo ...]

With no arguments, runs for every project in PROJECTS.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import PROJECTS, load_config
from agent.consolidation import run_consolidation
from agent.graph import open_checkpointer, open_store


async def main(repos: list[str]) -> int:
    """Returns the number of projects that failed, for the exit code."""
    config = load_config()
    failures: list[tuple[str, str]] = []
    async with open_checkpointer(config) as checkpointer, open_store(config) as store:
        for repo in repos:
            print(f"[consolidation] {repo}: running...", flush=True)
            try:
                summary = await run_consolidation(config, repo, checkpointer, store)
            except Exception as e:  # noqa: BLE001 -- one project's failure shouldn't abort the rest
                print(f"[consolidation] {repo}: FAILED -- {e}", flush=True)
                failures.append((repo, str(e)))
                continue
            print(f"[consolidation] {repo}: {summary}", flush=True)

    # Fail loudly. This used to print a line and exit 0, so cron stayed silent
    # and a broken nightly run looked identical to a healthy one -- the reason a
    # provider incompatibility went unnoticed for months. A non-zero exit gives
    # cron something to report, and the banner is greppable in the log.
    if failures:
        print("", flush=True)
        print("=" * 72, flush=True)
        print(f"CONSOLIDATION FAILED for {len(failures)} of {len(repos)} project(s):", flush=True)
        for repo, err in failures:
            print(f"  - {repo}: {err[:400]}", flush=True)
        print("=" * 72, flush=True)
        print("Memory was NOT updated for the projects above. This is not a no-op —", flush=True)
        print("episodes stay unconsolidated until this is fixed and re-run.", flush=True)
    return len(failures)


if __name__ == "__main__":
    repos = sys.argv[1:] or list(PROJECTS.keys())
    sys.exit(1 if asyncio.run(main(repos)) else 0)
