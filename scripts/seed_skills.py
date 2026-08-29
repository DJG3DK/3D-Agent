"""Seeds the repo's on-disk skills (skills/) into project skill stores.

The repo is the source of truth for authored and vendored skills; Postgres
is a deploy target. This fixes the state where hand-written skills (e.g.
grid-trading-architecture) existed ONLY in the store -- undiffable,
unreviewable, unrestorable. Re-running is the upgrade path: seed_skill
overwrites deliberately (curated content, same contract as
install_jspace_skill.py).

Each entry seeds one skill directory. Multi-file trees (webapp-testing)
are written file-by-file under /skills/<name>/<relpath>; only SKILL.md's
manifest registration goes through seed_skill so manifest bookkeeping
stays in one place. The description is parsed from SKILL.md frontmatter --
one source of truth, no drift between disk and manifest.

Usage: .venv/bin/python scripts/seed_skills.py
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skills that ship WITH the project, seeded into every configured repo.
# Deployment-specific skills are not listed here: they live in skills/local/
# (gitignored) and are discovered automatically, so a public checkout carries
# no one's private domain knowledge. See skills/local/README.md.
SHIPPED_SKILLS = ["vendor/webapp-testing"]

LOCAL_SKILLS_DIR = "local"
LOCAL_TARGETS_FILE = "targets.json"

SKIP_FILES = {"PROVENANCE.md", "LICENSE.txt"}


def _frontmatter_description(skill_md: str) -> str:
    m = re.search(r"^---\n(.*?)\n---", skill_md, re.DOTALL)
    if not m:
        raise ValueError("SKILL.md has no frontmatter block")
    fm = m.group(1)
    dm = re.search(r"^description:\s*(.+?)(?=^\w+:|\Z)", fm, re.DOTALL | re.MULTILINE)
    if not dm:
        raise ValueError("frontmatter has no description")
    # collapse folded/multi-line YAML values to one line, dropping a leading
    # block-scalar indicator (">", "|", ">-", ...) -- it's YAML syntax, not text
    words = dm.group(1).split()
    if words and words[0].rstrip("+-") in (">", "|"):
        words = words[1:]
    return " ".join(words)


def _seed_targets(skills_root: Path, all_repos: list[str]) -> dict[str, list[str]]:
    """What to seed where: the shipped skills, plus everything discovered in
    skills/local/. Discovery (rather than a hardcoded map) is what lets a
    deployment add its own skills without editing a tracked file."""
    targets: dict[str, list[str]] = {rel: list(all_repos) for rel in SHIPPED_SKILLS}

    local_root = skills_root / LOCAL_SKILLS_DIR
    scoping: dict[str, list[str]] = {}
    targets_file = local_root / LOCAL_TARGETS_FILE
    if targets_file.is_file():
        raw = json.loads(targets_file.read_text())
        scoping = {k: v for k, v in raw.items() if not k.startswith("_")}

    if local_root.is_dir():
        for child in sorted(local_root.iterdir()):
            if not (child / "SKILL.md").is_file():
                continue
            # Unscoped local skills go everywhere -- the common case is one
            # deployment whose skills apply to all of its own projects.
            targets[f"{LOCAL_SKILLS_DIR}/{child.name}"] = scoping.get(child.name, list(all_repos))
    return targets


async def main() -> None:
    from langgraph.store.postgres.aio import AsyncPostgresStore

    from agent.config import PROJECTS, load_config
    from agent.deep_agent import (
        SKILLS_ROUTE,
        route_local_path,
        seed_skill,
        skills_namespace,
    )
    from deepagents.backends.store import StoreBackend

    cfg = load_config()
    skills_root = Path(__file__).resolve().parent.parent / "skills"

    all_repos = list(PROJECTS)
    targets: dict[str, list[str]] = {}
    for rel_dir, repos in _seed_targets(skills_root, all_repos).items():
        targets[rel_dir] = repos

    async with AsyncPostgresStore.from_conn_string(cfg.pg_dsn) as store:
        for rel_dir, repos in targets.items():
            src = skills_root / rel_dir
            name = src.name
            skill_md = (src / "SKILL.md").read_text()
            description = _frontmatter_description(skill_md)
            extra_files = [
                p for p in sorted(src.rglob("*"))
                if p.is_file() and p.name != "SKILL.md" and p.name not in SKIP_FILES
            ]
            for repo in repos:
                await seed_skill(repo, store, name, description, skill_md)
                backend = StoreBackend(namespace=skills_namespace(repo), store=store)
                for p in extra_files:
                    rel = p.relative_to(src).as_posix()
                    await backend.awrite(
                        route_local_path(SKILLS_ROUTE, f"{SKILLS_ROUTE}{name}/{rel}"),
                        p.read_text(),
                    )
                print(f"seeded {name} -> {repo} (SKILL.md + {len(extra_files)} files)")


if __name__ == "__main__":
    asyncio.run(main())
