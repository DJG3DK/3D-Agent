"""Operator-tunable runtime limits, stored rather than baked into the image.

These were module constants and env vars, which meant every adjustment was a
file edit plus a restart -- and a restart is exactly what you cannot do while
the thing you want to retune is running. They now live in the same Postgres
store as skills and memory, so they survive restarts, ride along in the
backup, and take effect on the NEXT turn or task without one.

That last property is not incidental. Every value here is read at the point of
use -- inside build_planning_agent, build_deep_agent, or the verify_and_ship
node -- so a change lands on the next unit of work and never mutates something
already in flight. A task that started under a $2 ceiling finishes under it.

Deliberately NOT here: anything that changes what the system is allowed to do.
Auto-approve and merge review are per-user safety switches with their own
endpoints and their own reasoning. These are dials on how hard it tries before
giving up, which is a different kind of decision and a safe one to expose.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

NAMESPACE = ("settings",)
KEY = "runtime"

# name -> spec. `env` is the variable that seeded the old default, kept so an
# existing deployment's configuration is not silently discarded on upgrade.
KNOBS: dict[str, dict] = {
    "planning_turn_budget_usd": {
        "label": "Planning turn budget",
        "help": (
            "Dollar ceiling for a single planning turn, on top of what the session "
            "has already spent. Reaching it ends the turn -- the draft and the spend "
            "are both kept."
        ),
        "unit": "$",
        "default": 4.0,
        "min": 0.25,
        "max": 100.0,
        "env": "PLANNING_TURN_BUDGET_USD",
        "group": "Planning",
    },
    "planning_stall_timeout_s": {
        "label": "Planning stall timeout",
        "help": (
            "How long a planning turn may produce NO output before it is treated as "
            "hung. This is silence, not duration: a turn that keeps working runs as "
            "long as it needs. Raise it if a legitimate turn is ever cut off."
        ),
        "unit": "s",
        "default": 1200.0,
        "min": 120.0,
        "max": 7200.0,
        "env": "PLANNING_STALL_TIMEOUT_S",
        "group": "Planning",
    },
    "default_task_budget_usd": {
        "label": "Default task budget",
        "help": "Pre-filled ceiling for a new build task. Per-task, and overridable when you create one.",
        "unit": "$",
        "default": 2.0,
        "min": 0.25,
        "max": 100.0,
        "env": "DEFAULT_BUDGET_USD",
        "group": "Building",
    },
    "model_call_run_limit": {
        "label": "Model calls per run",
        "help": (
            "Backstop against a runaway loop, not a normal-operation cap -- a healthy "
            "task stays far below it. Applies to the coordinator and to each subagent."
        ),
        "unit": "calls",
        "default": 200.0,
        "min": 20.0,
        "max": 2000.0,
        "env": None,
        "group": "Building",
    },
    "tool_call_run_limit": {
        "label": "Tool calls per run",
        "help": "The same backstop for tool calls rather than model calls.",
        "unit": "calls",
        "default": 300.0,
        "min": 20.0,
        "max": 3000.0,
        "env": None,
        "group": "Building",
    },
    "review_wait_timeout_s": {
        "label": "Review wait timeout",
        "help": (
            "How long to wait for the independent review service on one commit. A real "
            "review of a large diff has been measured near 7 minutes, including a full "
            "dependency install and test suite, so leave headroom above that."
        ),
        "unit": "s",
        "default": 900.0,
        "min": 120.0,
        "max": 7200.0,
        "env": None,
        "group": "Building",
    },
}

# Seeded from env at import so a deployment that configured these the old way
# keeps its values, then overwritten by whatever the store holds.
_values: dict[str, float] = {}
for _name, _spec in KNOBS.items():
    _raw = os.environ.get(_spec["env"]) if _spec.get("env") else None
    try:
        _values[_name] = float(_raw) if _raw is not None else float(_spec["default"])
    except (TypeError, ValueError):
        _values[_name] = float(_spec["default"])


def value(name: str) -> float:
    """The current value. Sync on purpose: callers are deep inside agent
    construction, and a store round-trip per read would be a database call on
    a hot path for a number that changes a few times a year."""
    return _values.get(name, float(KNOBS[name]["default"]))


def as_int(name: str) -> int:
    return int(value(name))


def all_values() -> dict[str, float]:
    return dict(_values)


def clamp(name: str, raw: float) -> float:
    spec = KNOBS[name]
    return max(float(spec["min"]), min(float(spec["max"]), float(raw)))


async def load(store) -> None:
    """Read stored overrides into the cache. Called once at startup; a failure
    here must never stop the app booting -- the env/default seeding above is
    already a working configuration."""
    try:
        item = await store.aget(NAMESPACE, KEY)
    except Exception:  # noqa: BLE001 -- a settings read must not block startup
        logger.exception("could not load runtime settings; using defaults")
        return
    if not item or not isinstance(item.value, dict):
        return
    for name, raw in item.value.items():
        if name not in KNOBS:
            continue  # a knob removed in a later version; ignore rather than crash
        try:
            _values[name] = clamp(name, float(raw))
        except (TypeError, ValueError):
            logger.warning("ignoring unusable stored value for %s: %r", name, raw)


async def save(store, updates: dict[str, float]) -> dict[str, float]:
    """Validate, persist and apply. Unknown names are rejected rather than
    stored, so a typo cannot sit in the database looking like configuration."""
    unknown = sorted(set(updates) - set(KNOBS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    cleaned = {name: clamp(name, float(raw)) for name, raw in updates.items()}
    merged = {**_values, **cleaned}
    await store.aput(NAMESPACE, KEY, merged)
    _values.update(cleaned)
    return dict(_values)
