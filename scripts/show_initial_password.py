#!/usr/bin/env python
"""Print the first-run admin password once, then remove the file.

agent/server.py stores the generated password for the seeded admin account
encrypted with AUTH_SECRET_KEY in `.initial-admin-password` (mode 0600) at
the repo root -- never in the log. Run this from the repo root with the
same .env the agent uses:

    .venv/bin/python scripts/show_initial_password.py

The password must be changed on first login; the file is deleted after it
has been shown, so this works exactly once.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import auth  # noqa: E402
from agent.config import load_config  # noqa: E402

PATH = Path(__file__).resolve().parent.parent / ".initial-admin-password"


def main() -> int:
    if not PATH.exists():
        print(f"no {PATH.name} file: the initial password was already shown, or no admin was ever seeded", file=sys.stderr)
        return 1
    config = load_config()
    try:
        password = auth._decrypt_totp_secret(config, PATH.read_text().strip())
    except Exception:  # noqa: BLE001
        print("could not decrypt: AUTH_SECRET_KEY differs from the one the agent booted with", file=sys.stderr)
        return 2
    print(password)
    PATH.unlink()
    print(f"({PATH.name} removed; change this password on first login)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
