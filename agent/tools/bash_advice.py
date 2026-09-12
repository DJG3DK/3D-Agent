"""Noticing when a shell command was really a file edit or a file read.

Observed live on 2026-09-12, task 01e640ef: the test-writer subagent made 229
bash calls against ONE edit and ONE write. It was patching JavaScript by
piping Python heredocs -- `python3 - <<'PY' ... s = open(p).read();
open(p,'w').write(s.replace(...))` -- and reading files with `cat` and
`sed -n`, when it had `read`, `write` and `edit` tools the whole time.

That is not a stylistic preference. Every bash call spawns a fresh container:
the median gap between that subagent's model calls was 11 seconds, nearly all
of it container startup and teardown. `read`/`write`/`edit` run in-process and
cost none of it. The same work through the right tools is roughly an order of
magnitude faster in wall-clock, and it is also safer -- `edit` is path-guarded
to the repo root and its repeat-guard catches a model retrying an edit that
already failed, neither of which a shell heredoc gets.

So: a note on the result, never a refusal. Blocking would be wrong -- writing
a scratch script to RUN is a legitimate use of a shell, and the harness cannot
reliably tell the difference in every case. A short line pointing at the
cheaper tool costs one line of context and stops being emitted the moment the
model takes the hint.
"""

from __future__ import annotations

import re

# Writing a file through the shell.
_WRITE_PATTERNS = (
    # cat > file <<EOF  /  cat >> file
    re.compile(r"(?:^|[|;&]\s*)cat\s+>{1,2}\s*[\"']?([\w./-]+)", re.M),
    # tee file / tee -a file
    re.compile(r"\btee\s+(?:-a\s+)?[\"']?([\w./-]+)"),
    # in-place stream editors
    re.compile(r"\bsed\s+(?:-[a-zA-Z]*\s+)*-i\b"),
    re.compile(r"\bperl\s+-[a-zA-Z]*i"),
    # a python/node heredoc that opens a file for writing
    re.compile(r"open\s*\([^)]*['\"][wa]\+?['\"]\s*\)"),
    re.compile(r"\bwriteFileSync\s*\("),
    re.compile(r"\.write_text\s*\("),
)

# Reading a file through the shell, when nothing else is happening to it.
_READ_ONLY = re.compile(
    r"^\s*(?:cd\s+\S+\s*&&\s*)?"
    r"(?:cat|head|tail|sed\s+-n\s+['\"]?[\d,p$]+['\"]?)\s+"
    r"[\"']?([\w./-]+\.[\w]+)[\"']?\s*$"
)

EDIT_NOTE = (
    "[harness] That wrote a file through the shell. Use the `edit` tool (or `write` for a new "
    "file) instead: it runs in-process, while every bash call starts a container -- the shell "
    "route is ~10x the wall-clock for the same change, and `edit` is path-guarded and catches a "
    "repeated failed edit. Keep bash for RUNNING things: tests, builds, rg, git status."
)
READ_NOTE = (
    "[harness] That read a file through the shell. Use the `read` tool instead -- same content, "
    "no container, and it pages large files. Keep bash for RUNNING things."
)


def advice_for(command: str) -> str | None:
    """A one-line nudge, or None when the command is a fair use of a shell."""
    if not command or not command.strip():
        return None
    text = command.strip()

    # A write is the expensive mistake, so it wins when a command does both.
    for pattern in _WRITE_PATTERNS:
        if pattern.search(text):
            return EDIT_NOTE

    # Only for a command that does nothing BUT read one file. `cat x | grep y`
    # is a search, which is exactly what bash is for.
    if _READ_ONLY.match(text):
        return READ_NOTE
    return None


def kind(command: str) -> str | None:
    """"write" / "read" / None -- for counting these in tool telemetry."""
    note = advice_for(command)
    if note is EDIT_NOTE:
        return "write"
    if note is READ_NOTE:
        return "read"
    return None
