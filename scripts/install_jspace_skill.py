"""Seeds the vendored J-Space Cognition Suite (skills/vendor/j-space/, from
github.com/Tiger3807861189/J-Space-Cognition-Suite-V3.6, Apache-2.0;
security-reviewed before vendoring) into every project's skill store.

Unlike seed_skill (single SKILL.md per skill), this suite is a TREE --
SKILL.md + modules/ + references/ + scripts/ -- so every file is written
individually under /skills/j-space/<relpath>, and only SKILL.md's manifest
registration goes through seed_skill so the manifest bookkeeping stays in one
place. The .py scripts are seeded too: the store can't execute anything (it's
plain text either way), but the agent can materialize them into its sandbox
workspace with its own write tool if a task wants the optional controller.

Re-runnable by design: seed_skill overwrites deliberately (curated content),
and the tree writes do the same -- rerunning after a vendor update IS the
upgrade path, matching seed_skill's own contract.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepagents.backends import StoreBackend

from agent.config import PROJECTS, load_config
from agent.deep_agent import (
    SKILLS_ROUTE,
    route_local_path,
    seed_skill,
    skills_namespace,
)
from agent.outer_graph import open_store

VENDOR_DIR = Path(__file__).resolve().parent.parent / "skills" / "vendor" / "j-space"
SKILL_NAME = "j-space"
DESCRIPTION = (
    "Inference-time control for deep reasoning and long-horizon work: an entry "
    "gate picks a fast/full/loop pass and loads only the relevant modules "
    "(verification, recovery, focus, capacity). Read modules/ and references/ "
    "on demand; scripts/jspace.py is an optional workspace-state ledger."
)


async def main() -> None:
    config = load_config()
    files = sorted(p for p in VENDOR_DIR.rglob("*") if p.is_file())
    skill_md = (VENDOR_DIR / "SKILL.md").read_text(encoding="utf-8")
    async with open_store(config) as store:
        for repo in PROJECTS:
            # SKILL.md via seed_skill -- writes the file AND the manifest entry.
            await seed_skill(repo, store, SKILL_NAME, DESCRIPTION, skill_md)
            backend = StoreBackend(namespace=skills_namespace(repo), store=store)
            for f in files:
                rel = f.relative_to(VENDOR_DIR).as_posix()
                if rel == "SKILL.md":
                    continue
                key = route_local_path(SKILLS_ROUTE, f"{SKILLS_ROUTE}{SKILL_NAME}/{rel}")
                await backend.awrite(key, f.read_text(encoding="utf-8"))
            print(f"{repo}: seeded {len(files)} files under {SKILLS_ROUTE}{SKILL_NAME}/")


if __name__ == "__main__":
    asyncio.run(main())
