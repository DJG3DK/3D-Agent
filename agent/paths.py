"""Where this installation lives on disk.

Every path here used to be the literal string "/home/3d-agent", which is
where the original deployment happened to sit. That works exactly once: a
second install anywhere else silently reads and writes the first one's files,
or fails on a directory that isn't there.

REPO_ROOT is derived from this file's own location (agent/paths.py -> agent/
-> repo root), so it is correct wherever the checkout is, including a git
worktree of it. AGENT_HOME overrides it for the unusual case of running the
code from one place while its data lives in another.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT: Path = Path(
    os.environ.get("AGENT_HOME") or Path(__file__).resolve().parent.parent
).resolve()

# Runtime data the app writes (gitignored). Kept together so a deployment can
# point them somewhere else in one move.
DATA_DIR: Path = REPO_ROOT / "data"
SERVICES_DIR: Path = REPO_ROOT / "services"

# The review services' own on-disk state.
REVIEWER_DIR: Path = SERVICES_DIR / "commit-reviewer"
REVIEWER_USAGE_LOG: Path = REVIEWER_DIR / "usage.jsonl"


def repo_path(*parts: str) -> Path:
    """A path inside this installation."""
    return REPO_ROOT.joinpath(*parts)
