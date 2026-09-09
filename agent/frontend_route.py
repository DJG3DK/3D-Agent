"""Frontend routing: which planning chat and which coder a request gets.

The operator wants Kimi k3 on frontend work -- "a master at frontend polish"
-- and deepseek everywhere else, because Kimi costs roughly ten times as much
per coder call. Polish is decided at the keyboard, not in the plan, so the
seat that matters is the coder (and the investigator that reads for it); the
test-writer stays on the general model by the operator's own call
(2026-09-09). Planning chat gets a frontend tier too, so a frontend plan is
written in the vocabulary the coder will execute it in.

Three signals, strongest first, plus a switch the operator flips:

1. category  -- the task classifier's `ui-styling` is the strongest evidence:
                a model read the whole goal. (Tasks only; a planning session
                has no category when its first turn starts.)
2. paths     -- files the request names. Mostly frontend paths is frontend
                work whatever the category says: a `feature` that lives in
                frontend/ routes to Kimi. Mostly backend paths stays general.
3. keywords  -- a short list, and it takes two distinct hits: "fix the chart's
                numbers" mentions a chart but is a data bug.
0. override  -- "frontend" or "general" from the Build Now popup, the New Task
                form, or a new planning session. Beats everything.

Every decision carries a reason, and the task/session shows it, because
silent routing is how a $40 Kimi run on a backend refactor would happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUTES = ("auto", "frontend", "general")
FRONTEND, GENERAL = "frontend", "general"

# Roles the two routes resolve to. The aliases live in the router's
# config.yaml; the operator pins whatever model they like behind them.
CODER_ROLE = {FRONTEND: "agent-coder-frontend", GENERAL: "agent-coder"}
PLANNING_ROLE = {FRONTEND: "agent-planning-chat-frontend"}

FRONTEND_CATEGORIES = frozenset({"ui-styling"})
FRONTEND_DIRS = frozenset({"frontend", "web", "client", "ui", "components", "pages", "views", "layouts", "styles", "css"})
FRONTEND_EXTS = frozenset({".tsx", ".jsx", ".css", ".scss", ".less", ".html", ".vue", ".svelte"})
CODE_EXTS = FRONTEND_EXTS | frozenset({".ts", ".js", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".rb", ".php", ".sql", ".sh", ".json", ".yaml", ".yml"})
FRONTEND_KEYWORDS = (
    "ui", "ux", "layout", "styling", "style", "css", "design", "responsive", "theme",
    "animation", "polish", "dashboard", "page", "component", "button", "modal",
    "sidebar", "font", "color", "colour", "spacing", "mobile", "dark mode", "hover", "tooltip",
)

_PATH_TOKEN = re.compile(r"(?<![\w/])((?:[\w.@-]+/)+[\w.@-]+|[\w@-]+\.(?:tsx|jsx|css|scss|less|html|vue|svelte))(?![\w/])")


@dataclass(frozen=True)
class RouteDecision:
    route: str      # "frontend" | "general"
    reason: str

    @property
    def is_frontend(self) -> bool:
        return self.route == FRONTEND


def _is_frontend_path(path: str) -> bool:
    parts = [p.lower() for p in path.strip("`'\"()[],.").split("/")]
    ext = "." + parts[-1].rsplit(".", 1)[-1] if "." in parts[-1] else ""
    if ext in FRONTEND_EXTS:
        return True
    if ext in CODE_EXTS and any(p in FRONTEND_DIRS for p in parts[:-1]):
        return True
    return False


def _is_code_path(path: str) -> bool:
    last = path.strip("`'\"()[],.").split("/")[-1]
    return "." in last and ("." + last.rsplit(".", 1)[-1]) in CODE_EXTS


def named_paths(text: str) -> tuple[list[str], list[str]]:
    """(frontend paths, other code paths) named in the text, de-duplicated."""
    fe: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for tok in _PATH_TOKEN.findall(text or ""):
        tok = tok.strip("`'\"()[],.")
        if tok in seen or "://" in tok:
            continue
        seen.add(tok)
        if _is_frontend_path(tok):
            fe.append(tok)
        elif _is_code_path(tok):
            other.append(tok)
    return fe, other


def keyword_hits(text: str) -> list[str]:
    lowered = (text or "").lower()
    hits = []
    for kw in FRONTEND_KEYWORDS:
        if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", lowered):
            hits.append(kw)
    return hits


def classify_frontend(text: str, category: str | None = None, override: str | None = None) -> RouteDecision:
    """The routing decision for a task goal, a plan, or a planning message."""
    if override in (FRONTEND, GENERAL):
        return RouteDecision(override, "operator's choice")
    if category in FRONTEND_CATEGORIES:
        return RouteDecision(FRONTEND, f"category {category}")
    fe, other = named_paths(text)
    if fe and len(fe) >= len(other):
        return RouteDecision(FRONTEND, f"{len(fe)} of {len(fe) + len(other)} named files are frontend")
    if fe and len(other) > len(fe):
        return RouteDecision(GENERAL, f"mixed: {len(other)} backend vs {len(fe)} frontend files")
    hits = keyword_hits(text)
    if len(hits) >= 2:
        return RouteDecision(FRONTEND, "keywords: " + ", ".join(hits[:4]))
    return RouteDecision(GENERAL, "no frontend signal")


def normalize_override(value: str | None) -> str | None:
    """A request's `route` field: "frontend"/"general" are overrides, "auto"
    or missing means decide."""
    return value if value in (FRONTEND, GENERAL) else None
