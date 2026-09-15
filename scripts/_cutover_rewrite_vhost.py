"""Point the OLD agent vhost at tektonix.io. Called by cutover_tektonix.sh.

Surgical on purpose. agent.3dcryptobots.com carries three things:

    /            and /v2/   -- the dashboard, which is what moves
    /_login, /_login/api/   -- a DIFFERENT tool's login, on :14001
    /_auth_verify, /_review/ -- the review/merge UI, on :4100

A blanket `return 301` on that server block would take the last two offline,
so only the two dashboard locations are rewritten and everything else is left
byte-for-byte alone.

Idempotent: running it twice is a no-op.
"""

from __future__ import annotations

import sys

NEW_ROOT = '    location / {\n        return 301 https://tektonix.io$request_uri;\n    }'
OLD_ROOT = '    location / {\n        return 302 /v2/;\n    }'
NEW_V2 = '    location /v2/ {\n        return 301 https://tektonix.io/;\n    }'
MARKER = "    location /v2/ {"


def _replace_block(src: str, marker: str, replacement: str) -> str:
    """Swap out a whole nginx location block, found by brace matching.

    Not a regex: the /v2/ block holds nested braces and ~30 lines of comment,
    and any greedy pattern would swallow the rest of the file. Brace matching
    is the only way to find its real end.
    """
    start = src.find(marker)
    if start == -1:
        return src
    depth = 0
    i = src.index("{", start)
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[:start] + replacement + src[i + 1:]
        i += 1
    raise SystemExit("unbalanced braces in the /v2/ block -- refusing to write")


def main(path: str) -> int:
    with open(path) as fh:
        src = before = fh.read()

    if "tektonix.io" in src:
        print("  old vhost already redirects -- nothing to do")
        return 0

    src = src.replace(OLD_ROOT, NEW_ROOT)
    # Once vite's base is "/", a page served under /v2/ requests every asset
    # from the root and 404s, so this block must stop proxying.
    src = _replace_block(src, MARKER, NEW_V2)

    if src == before:
        print("  WARNING: neither location matched -- vhost left unchanged", file=sys.stderr)
        return 1

    with open(path, "w") as fh:
        fh.write(src)
    print("  / and /v2/ now 301 to tektonix.io; /_login and /_review untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
