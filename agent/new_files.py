"""Paths a task names that do not exist yet.

A build task is the one shape where "not found" is the CORRECT answer, and
nothing in the system said so. Live on 2026-09-14, task 3ee0d030: the goal
opened "Add a new strategy `multiTrader`" and gave a table of three files to
create. The agent spent 1h49m, $6.98 and 754 tool calls, wrote nothing, and
stopped to ask the operator about "a fundamental problem I need to resolve
with you rather than guess at". There was no problem -- the worktree was at
the same commit as live main with every referenced file present. The only
thing missing was the files it had been asked to create.

Seven subagent delegations made it worse rather than better. Each one starts
with a fresh context: an investigator told to look at `multiTrader` searches,
finds nothing, and correctly reports absence. The coordinator hears "it does
not exist" from one subagent after another and reads a blocker, because
nothing ever told either of them that absence was the point.

So the absence is computed once, from the goal, and stated. Not inferred by
the model from prose it has already read -- named, as a fact, in the system
prompt of the coordinator and of every subagent that might go looking.
"""

from __future__ import annotations

import os
import re

# Conservative on purpose. A path here becomes a line in the system prompt, so
# a false positive costs prompt space and credibility; a false negative just
# leaves today's behaviour. Requires a directory separator and a code-ish
# extension, which is what a build plan's file table actually contains.
_EXT = (
    "js|jsx|mjs|cjs|ts|tsx|py|go|rs|rb|php|java|cs|ex|exs|"
    "css|scss|html|json|yaml|yml|toml|md|sql|sh"
)
_PATH = re.compile(rf"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:{_EXT}))(?![\w/])")

# A plan can name a lot of files; the prompt should not turn into a manifest.
MAX_LISTED = 25


def declared_paths(goal: str) -> list[str]:
    """Repo-relative paths the goal mentions, in first-appearance order."""
    if not goal:
        return []
    seen: list[str] = []
    for m in _PATH.finditer(goal):
        p = m.group(1).strip().lstrip("./")
        # A URL's path is not a repo path.
        start = max(0, m.start() - 8)
        if "//" in goal[start:m.start()]:
            continue
        if p not in seen:
            seen.append(p)
    return seen


def absent_paths(repo_root: str, paths: list[str]) -> list[str]:
    """Those that are not on disk. Missing repo_root means we cannot tell, and
    claiming a file is absent when we simply could not look would be worse
    than saying nothing."""
    if not repo_root or not os.path.isdir(repo_root):
        return []
    return [p for p in paths if not os.path.exists(os.path.join(repo_root, p))]


def guidance(repo_root: str, goal: str) -> str:
    """The prompt block, or "" when every named path already exists."""
    missing = absent_paths(repo_root, declared_paths(goal))
    if not missing:
        return ""
    listed = missing[:MAX_LISTED]
    more = len(missing) - len(listed)
    lines = "\n".join(f"  - {p}" for p in listed)
    tail = f"\n  ...and {more} more" if more else ""
    return (
        "\n\nPATHS THIS TASK NAMES THAT DO NOT EXIST YET (checked on disk just now):\n"
        f"{lines}{tail}\n"
        "Their absence is the EXPECTED starting state for anything you were asked to create. "
        "Finding one missing is a confirmation, not a discovery, and never a reason to stop and "
        "ask the operator -- a task that says \"add\", \"create\" or \"new\" is telling you the "
        "file is not there yet. Do not spend turns searching for them, and do not delegate a "
        "subagent to look for them. If you believe the task actually expected one of these to "
        "exist already, say that explicitly in your final report rather than treating it as a "
        "blocker mid-run."
    )
