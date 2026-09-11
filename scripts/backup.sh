#!/usr/bin/env bash
# Back up everything that cannot be rebuilt from the repo.
#
# One Postgres database holds all of it: tasks and their checkpoints, planning
# sessions, project and org memory, episodes, runtime limits, the GitHub inbox
# and its encrypted tokens, users, sessions and 2FA secrets. Lose it and the
# box still runs -- with no history, no memory and no accounts.
#
# The dump is useless on its own. AUTH_SECRET_KEY (in .env) is what decrypts
# the TOTP secrets and the stored GitHub tokens, so a restore onto a box with a
# different key comes back with unreadable secrets and locked-out 2FA. Keep the
# .env beside the dump, or at least keep that one value somewhere you trust.
#
#   scripts/backup.sh [destination-dir]      default: $AGENT_HOME/backups
#
# Restoring is documented in docs/backup.md, and exercised end to end by
# scripts/verify_backup_restore.sh, which restores into a scratch database and
# checks the rows are really there.
set -euo pipefail

AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEST="${1:-$AGENT_HOME/backups}"
KEEP="${BACKUP_KEEP:-14}"          # how many dumps to retain

cd "$AGENT_HOME"
# shellcheck disable=SC1091
set -a; . ./.env; set +a
: "${LANGGRAPH_PG_DSN:?LANGGRAPH_PG_DSN is not set -- run this with the agent .env loaded}"

mkdir -p "$DEST"
chmod 700 "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DEST/agent-$STAMP.dump"

# Custom format (-Fc): compressed, and restorable table by table with pg_restore.
# --no-owner so a restore into a differently-named role works.
pg_dump --dbname="$LANGGRAPH_PG_DSN" --format=custom --no-owner --file="$OUT"
chmod 600 "$OUT"

# A dump that cannot be listed is not a backup. This catches a truncated or
# half-written file now, rather than on the day you need it.
TABLES=$(pg_restore --list "$OUT" | grep -c 'TABLE DATA' || true)
if [ "$TABLES" -lt 5 ]; then
    echo "backup: $OUT lists only $TABLES tables -- refusing to call that a backup" >&2
    exit 1
fi

SIZE=$(du -h "$OUT" | cut -f1)
echo "backup: wrote $OUT ($SIZE, $TABLES tables)"

# Keep the last N. Deliberately by name, which sorts chronologically.
mapfile -t OLD < <(ls -1 "$DEST"/agent-*.dump 2>/dev/null | head -n "-$KEEP" || true)
for f in "${OLD[@]:-}"; do
    [ -n "$f" ] || continue
    rm -f "$f"
    echo "backup: pruned $(basename "$f")"
done
