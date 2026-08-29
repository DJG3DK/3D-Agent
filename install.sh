#!/usr/bin/env bash
#
# 3D-Agent installer.
#
# Asks for what it cannot derive, derives everything else, and shows you each
# step before it runs. Safe to re-run: every step checks whether it already
# happened, and it never overwrites an existing .env or projects.json.
#
#   ./install.sh                 interactive (recommended)
#   ./install.sh --dry-run       show what would happen, change nothing
#   ./install.sh --yes           non-interactive; reads answers from the
#                                environment (see --help)
#
set -uo pipefail

AGENT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$AGENT_HOME" || exit 1

DRY_RUN=0
ASSUME_YES=0

# --- output -----------------------------------------------------------------
if [ -t 1 ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
    B=""; DIM=""; R=""; G=""; Y=""; N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$B" "$N" "$B" "$*$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$N"; }

usage() {
    cat <<'EOF'
3D-Agent installer

  ./install.sh [--dry-run] [--yes] [--help]

  --dry-run   Print every action without performing it.
  --yes       Non-interactive. Answers come from the environment:
                PG_DSN              Postgres DSN (required)
                OPENROUTER_API_KEY  OpenRouter key (required)
                ADMIN_EMAIL         first admin account (default admin@example.com)
                SKIP_DOCKER=1       don't build the sandbox image
                SKIP_FRONTEND=1     don't build the dashboard bundle
EOF
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done

run() {
    if [ "$DRY_RUN" = "1" ]; then
        note "would run: $*"
        return 0
    fi
    "$@"
}

# Ask a question with a default. In --yes mode the default is taken silently,
# which is why every REQUIRED answer is validated separately rather than
# defaulted to something wrong.
ask() {
    local prompt="$1" default="${2:-}" reply
    if [ "$ASSUME_YES" = "1" ]; then
        printf '%s' "$default"
        return
    fi
    if [ -n "$default" ]; then
        read -r -p "  $prompt [$default]: " reply </dev/tty
        printf '%s' "${reply:-$default}"
    else
        read -r -p "  $prompt: " reply </dev/tty
        printf '%s' "$reply"
    fi
}

confirm() {
    local prompt="$1"
    [ "$ASSUME_YES" = "1" ] && return 0
    local reply
    read -r -p "  $prompt [y/N] " reply </dev/tty
    [[ "$reply" =~ ^[Yy] ]]
}

# --- 0. preflight -----------------------------------------------------------
step "Checking prerequisites"

missing=0
need() {
    local cmd="$1" why="$2"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd — $(command -v "$cmd")"
    else
        warn "$cmd not found — $why"
        missing=1
    fi
}

need git    "required to create per-project worktrees"
need python3 "the agent runs on Python 3.12+"
need node   "the dashboard build and the review services need Node 20+"
need docker "the agent's bash/edit tools run inside a container; without it the FIRST tool call of the first task fails"

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,12) else 0)' 2>/dev/null || echo 0)
[ "$PY_OK" = "1" ] || { warn "python3 is $(python3 -V 2>&1 | cut -d" " -f2); 3.12+ required"; missing=1; }

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
[ "$NODE_MAJOR" -ge 20 ] 2>/dev/null || { warn "node is v$NODE_MAJOR; 20+ required"; missing=1; }

if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    warn "docker is installed but not usable by this user (try: sudo usermod -aG docker \$USER, then re-login)"
    missing=1
fi

command -v pm2 >/dev/null 2>&1 && ok "pm2 — optional, for running as a service" \
                               || note "pm2 not found (optional: npm i -g pm2 to run as a managed service)"

[ "$missing" = "0" ] || die "install the missing prerequisites above, then re-run."

# --- 1. answers -------------------------------------------------------------
step "Configuration"

if [ -f .env ]; then
    ok ".env already exists — keeping it (delete it to start over)"
    ENV_EXISTS=1
else
    ENV_EXISTS=0
    say "  Three answers are needed. Everything else is generated."
    say ""

    PG_DSN="${PG_DSN:-}"
    [ -n "$PG_DSN" ] || PG_DSN=$(ask "Postgres DSN" "postgresql://postgres@localhost:5432/three_d_agent")
    [ -n "$PG_DSN" ] || die "a Postgres DSN is required"

    OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
    if [ -z "$OPENROUTER_API_KEY" ]; then
        say "  An OpenRouter key is the only paid dependency (openrouter.ai/keys)."
        OPENROUTER_API_KEY=$(ask "OpenRouter API key")
    fi
    [ -n "$OPENROUTER_API_KEY" ] || die "an OpenRouter API key is required"

    ADMIN_EMAIL="${ADMIN_EMAIL:-}"
    [ -n "$ADMIN_EMAIL" ] || ADMIN_EMAIL=$(ask "Email for the first admin account" "admin@example.com")
fi

# --- 2. secrets -------------------------------------------------------------
if [ "$ENV_EXISTS" = "0" ]; then
    step "Generating secrets"
    # AUTH_SECRET_KEY must decode to 16/24/32 RAW bytes. `openssl rand -hex 32`
    # yields 64 hex CHARACTERS, which decodes to 48 bytes and is rejected at
    # startup -- a documented footgun, so the installer generates it correctly
    # rather than leaving it to a copy-paste.
    if [ "$DRY_RUN" = "1" ]; then
        AUTH_SECRET_KEY="<generated>"; LITELLM_MASTER_KEY="<generated>"
        note "would generate AUTH_SECRET_KEY (32 raw bytes, urlsafe-base64) and LITELLM_MASTER_KEY"
    else
        AUTH_SECRET_KEY=$(python3 -c 'import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
        LITELLM_MASTER_KEY="sk-$(python3 -c 'import secrets;print(secrets.token_hex(24))')"
        ok "AUTH_SECRET_KEY generated (32 raw bytes — the format the app actually requires)"
        ok "LITELLM_MASTER_KEY generated (shared by the agent and the router)"
    fi
fi

# --- 3. write config --------------------------------------------------------
step "Writing configuration"

if [ "$ENV_EXISTS" = "0" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        note "would write .env and services/llm-router/.env"
    else
        umask 077
        cat > .env <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Never commit this file.
LANGGRAPH_PG_DSN=$PG_DSN

# The router this agent's model aliases resolve through (services/llm-router).
LITELLM_BASE_URL=http://127.0.0.1:4000
LITELLM_API_KEY=$LITELLM_MASTER_KEY

# Required at startup; the current pipeline routes through the agent-* aliases
# in services/llm-router/config.yaml instead of reading these.
MODEL_PLAN=agent-planner
MODEL_EXECUTE=agent-coder
MODEL_REFLECT=agent-reviewer

DEFAULT_BUDGET_USD=5.0
API_PORT=8100

AUTH_SECRET_KEY=$AUTH_SECRET_KEY
ADMIN_EMAIL=$ADMIN_EMAIL

# Where projects may be onboarded from, and where their worktrees are created.
# Onboarding gives an agent write access to what it points at, so keep this
# as narrow as your layout allows.
AGENT_PROJECT_ROOTS=$HOME
AGENT_SANDBOX_ROOT=$HOME/agent-workspaces

# Optional: password-reset email. Leave blank to disable.
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=

LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=
LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S=30
EOF
        cat > services/llm-router/.env <<EOF
# Generated by install.sh. Never commit this file.
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY
EOF
        umask 022
        ok "wrote .env and services/llm-router/.env (mode 600)"
    fi
fi

if [ -f projects.json ]; then
    ok "projects.json already exists — keeping it"
else
    if [ "$DRY_RUN" = "1" ]; then
        note "would create an empty projects.json"
    else
        printf '{\n  "projects": {}\n}\n' > projects.json
        ok "created an empty projects.json — add projects from the dashboard or scripts/add_project.py"
    fi
fi

# --- 4. database ------------------------------------------------------------
step "Database"

DB_NAME=$(printf '%s' "${PG_DSN:-}" | sed -n 's|.*/\([^/?]*\)\(?.*\)\{0,1\}$|\1|p')
if [ "$ENV_EXISTS" = "1" ]; then
    PG_DSN=$(grep -E '^LANGGRAPH_PG_DSN=' .env | cut -d= -f2-)
    DB_NAME=$(printf '%s' "$PG_DSN" | sed -n 's|.*/\([^/?]*\)\(?.*\)\{0,1\}$|\1|p')
fi

if [ "$DRY_RUN" = "1" ]; then
    note "would verify the database is reachable and create '$DB_NAME' if missing"
elif command -v psql >/dev/null 2>&1; then
    # -w: never prompt for a password. Without it psql blocks on a hidden
    # prompt when the DSN omits one -- which hangs an unattended install with
    # no visible reason. PGCONNECT_TIMEOUT bounds an unreachable host.
    if PGCONNECT_TIMEOUT=5 psql -w "$PG_DSN" -c 'SELECT 1' >/dev/null 2>&1; then
        ok "connected to $DB_NAME"
    else
        warn "cannot connect to $DB_NAME"
        if command -v createdb >/dev/null 2>&1 && confirm "create database '$DB_NAME' now?"; then
            if PGCONNECT_TIMEOUT=5 createdb -w "$DB_NAME" 2>/dev/null; then
                ok "created $DB_NAME"
            else
                warn "createdb failed — create it yourself, then re-run:"
                note "sudo -u postgres createdb $DB_NAME"
                note "and make sure the DSN in .env can authenticate (password, or a peer/trust entry in pg_hba.conf)"
            fi
        else
            warn "create the database yourself before starting the agent"
        fi
    fi
else
    note "psql not found — skipping the connection check (the agent will create its own tables on first start)"
fi

# --- 5. python --------------------------------------------------------------
step "Python environment"

if [ -d .venv ]; then
    ok ".venv already exists"
else
    run python3 -m venv .venv && ok "created .venv"
fi
if [ "$DRY_RUN" = "1" ]; then
    note "would install requirements.txt into .venv"
else
    say "  installing dependencies (this takes a minute)…"
    .venv/bin/pip install -q --upgrade pip >/dev/null 2>&1
    if .venv/bin/pip install -q -r requirements.txt; then
        ok "agent dependencies installed"
    else
        die "pip install failed — see the output above"
    fi
fi

if [ -d services/llm-router/venv ]; then
    ok "router venv already exists"
elif [ "$DRY_RUN" = "1" ]; then
    note "would create services/llm-router/venv and install its requirements"
else
    python3 -m venv services/llm-router/venv \
        && services/llm-router/venv/bin/pip install -q -r services/llm-router/requirements.txt \
        && ok "router dependencies installed" \
        || warn "router dependency install failed — see services/llm-router/requirements.txt"
fi

# --- 6. sandbox image -------------------------------------------------------
step "Sandbox container image"

if [ "${SKIP_DOCKER:-0}" = "1" ]; then
    warn "skipped (SKIP_DOCKER=1) — the first tool call of the first task will fail without it"
elif [ "$DRY_RUN" = "1" ]; then
    note "would build 3d-agent-sandbox:latest from docker/agent-sandbox/"
elif docker image inspect 3d-agent-sandbox:latest >/dev/null 2>&1; then
    ok "3d-agent-sandbox:latest already built"
else
    say "  building 3d-agent-sandbox:latest (a few minutes; it includes headless Chromium so the agent can see the UIs it builds)…"
    if docker build -q -t 3d-agent-sandbox:latest docker/agent-sandbox/ >/dev/null; then
        ok "sandbox image built"
    else
        warn "sandbox build failed — run it yourself: docker build -t 3d-agent-sandbox:latest docker/agent-sandbox/"
    fi
fi

# --- 7. frontend ------------------------------------------------------------
step "Dashboard"

if [ "${SKIP_FRONTEND:-0}" = "1" ]; then
    warn "skipped (SKIP_FRONTEND=1) — the server has no UI to serve until you run: cd frontend && npm ci && npm run build"
elif [ "$DRY_RUN" = "1" ]; then
    note "would run npm ci && npm run build in frontend/"
elif [ -f frontend/dist/index.html ] && [ -d frontend/node_modules ]; then
    ok "dashboard already built"
else
    say "  installing and building the dashboard…"
    if (cd frontend && npm ci --silent >/dev/null 2>&1 && npm run build >/dev/null 2>&1); then
        ok "dashboard built to frontend/dist"
    else
        warn "dashboard build failed — run it yourself: cd frontend && npm ci && npm run build"
    fi
fi

# --- done -------------------------------------------------------------------
step "Done"

if [ "$DRY_RUN" = "1" ]; then
    say "  Dry run — nothing was changed."
    exit 0
fi

cat <<EOF

  Start the router, then the agent:

    ${B}services/llm-router/venv/bin/litellm --config services/llm-router/config.yaml --port 4000${N}
    ${B}.venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8100${N}

  Or under pm2:

    ${B}pm2 start ecosystem.config.js && pm2 start services/llm-router/ecosystem.config.js${N}

  Then open ${B}http://127.0.0.1:8100${N}. The first admin password is printed
  ONCE to the server log on first startup — capture it.

  Finally, add a project to work on: Settings → Projects in the dashboard, or
    ${B}.venv/bin/python scripts/add_project.py /path/to/your/repo${N}

  Full guide: INSTALL.md
EOF
