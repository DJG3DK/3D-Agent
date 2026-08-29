"""Add a project from the command line -- the wizard's headless twin.

Same three phases as the dashboard wizard (inspect -> confirm -> provision)
and the same module underneath (agent/provisioning.py), so a headless install
cannot drift from what the UI does.

    .venv/bin/python scripts/add_project.py /path/to/repo
    .venv/bin/python scripts/add_project.py /path/to/repo --yes

--yes accepts the RECOMMENDED answers, which deliberately means: proposed
secret files on, fixture mounts off, and any test script that makes network
calls left OFF. It never auto-enables something this tool could not verify --
see agent/provisioning.py for why that distinction is the whole point.

The project must sit inside AGENT_PROJECT_ROOTS (default /home) and its name
is the directory's own basename; the worktree location comes from
AGENT_SANDBOX_ROOT. Same containment rules as the dashboard wizard, because
both call the same module.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import _PROJECTS_CONFIG_PATH, PROJECTS  # noqa: E402
from agent import provisioning as prov  # noqa: E402


def _ask(question: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  {question} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _choose(items, assume_yes: bool, label: str) -> list[str]:
    chosen = []
    if not items:
        return chosen
    print(f"\n{label}")
    for c in items:
        print(f"  - {c.value}\n      {c.reason}")
        if c.warning:
            print(f"      ! {c.warning}")
        chosen.append(c.value) if (c.enabled if assume_yes else _ask(f"include {c.value}?", c.enabled)) else None
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description="Onboard a project for the agent")
    ap.add_argument("path", help="absolute path to the live repo")
    ap.add_argument("--sandbox-root", default=None,
                    help="override AGENT_SANDBOX_ROOT for this run")
    ap.add_argument("--yes", action="store_true", help="accept recommended answers")
    args = ap.parse_args()

    try:
        report = prov.detect_project(args.path, sandbox_root=args.sandbox_root,
                                     existing_names=list(PROJECTS))
    except prov.ProvisioningError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    name = report.name
    print(f"\n{name}")
    print(f"  live      {report.live}")
    print(f"  worktree  {report.sandbox}")
    print(f"  stack     {', '.join(report.languages) or 'unknown'}"
          + (f" ({report.package_manager})" if report.package_manager else ""))

    for b in report.blockers:
        print(f"  BLOCKER   {b}")
    if report.blockers:
        return 1
    for w in report.warnings:
        print(f"  warning   {w}")

    print("\nChecks the review gate will run:")
    for c in report.checks:
        print(f"  {c['name']:<12} {c['cmd']} {' '.join(c['args'])}")
    if not report.checks:
        print("  (none detected)")

    secrets = _choose(report.secret_files, args.yes,
                      "Secret files to copy into review checkouts:")
    mounts = _choose(report.read_only_mounts, args.yes,
                     "Read-only fixture mounts:")
    apps = _choose(report.pm2_apps, args.yes, "pm2 apps to restart on deploy:")
    risky = _choose(report.risky_scripts, args.yes,
                    "Test scripts that make network calls (off unless you confirm):")

    checks = list(report.checks) + [
        {"name": r, "dir": ".", "cmd": report.package_manager or "npm",
         "args": ["run", r], "timeoutMs": 900_000}
        for r in risky
    ]

    if not args.yes and not _ask(f"\ncreate project {name!r}?", True):
        print("aborted")
        return 1

    ok, detail = prov.create_worktree(report.live, report.sandbox)
    print(f"  worktree  {'ok' if ok else 'FAILED'}: {detail}")
    if not ok:
        return 1

    entry = prov.config_from_choices(name, report.live, report.sandbox, {
        "secret_files": secrets, "read_only_mounts": mounts, "pm2_apps": apps,
        "node_modules_dirs": report.node_modules_dirs, "checks": checks,
        "build_steps": report.build_steps, "db_env_file": report.db_env_file,
    })
    try:
        prov.write_project_entry(_PROJECTS_CONFIG_PATH, name, entry)
    except prov.ProvisioningError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"  config    wrote {name} to {_PROJECTS_CONFIG_PATH}")
    print(json.dumps(entry, indent=2))
    print("\nNext:")
    print(f"  .venv/bin/python scripts/run_cartographer.py {name}   # build its codebase map")
    print(f"  .venv/bin/python scripts/seed_memory.py               # seed project memory")
    print("  restart the agent so the running process picks it up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
