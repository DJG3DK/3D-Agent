"""The deployment table, read from the operator's own config.yaml.

Same file litellm used and the dashboard's Models page still writes to. That
is the whole migration story: none. Point LITELLM_BASE_URL at this service and
every pin, cost figure and fallback rule carries over untouched.

Reloaded on change rather than at startup. litellm read the file once, so
repinning a model needed a router restart, and a restart kills every model call
in flight across every service sharing the router -- the one operation
docs/architecture.md tells you never to do while a task is running. Here the
file's mtime is checked per request (a stat, ~microseconds) and a changed file
is swapped in atomically between requests. In-flight calls keep the table they
started with.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("model-router")

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MODEL_ROUTER_CONFIG")
    or "/home/3d-agent/services/llm-router/config.yaml"
)
# Applies when a deployment does not set its own. litellm defaulted to none at
# all, which is how one upstream call ran 1802 seconds for 280 output tokens.
DEFAULT_TIMEOUT_S = float(os.environ.get("MODEL_ROUTER_TIMEOUT_S", "600"))


@dataclass(frozen=True)
class Deployment:
    """One `- model_name:` entry."""

    alias: str
    model: str                      # provider-qualified, e.g. "deepseek/deepseek-v4.1-flash"
    extra_body: dict = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


@dataclass(frozen=True)
class Table:
    """An immutable snapshot. Swapped wholesale, never mutated, so a request
    that started under one version finishes under it."""

    deployments: dict[str, Deployment]
    fallbacks: dict[str, list[str]]
    loaded_at: float
    mtime: float
    source: str

    def resolve(self, alias: str) -> Deployment | None:
        return self.deployments.get(alias)

    def chain(self, alias: str) -> list[str]:
        """The alias itself, then its fallbacks -- each one only if it exists.

        A fallback naming a deployment that is not in the table is dropped
        rather than attempted: litellm answered that case with "Available Model
        Group Fallbacks=None" at the moment of failure, which is the worst
        possible time to find out.
        """
        out = [alias]
        for name in self.fallbacks.get(alias, []):
            if name in self.deployments and name not in out:
                out.append(name)
        return out


def _strip_provider_prefix(model: str) -> str:
    """`openrouter/deepseek/deepseek-v4.1-flash` -> `deepseek/deepseek-v4.1-flash`.

    litellm used the leading segment to pick an SDK. We only ever talk to
    OpenRouter, so it is noise -- but it stays in the file, because the file is
    the operator's and the Models page writes that form.
    """
    return model.split("/", 1)[1] if model.startswith("openrouter/") else model


def load(path: Path | None = None) -> Table:
    p = Path(path or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(p.read_text()) or {}
    deployments: dict[str, Deployment] = {}

    for entry in raw.get("model_list") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("model_name")
        params = entry.get("litellm_params") or {}
        model = params.get("model")
        if not alias or not model:
            continue
        if alias in deployments:
            # litellm load-balances duplicate model_names across deployments.
            # Nothing here has relied on that since the tier pools were removed
            # (2026-09-13) -- every alias is one model. Keeping the first and
            # saying so beats silently picking one.
            logger.warning("duplicate deployment %s; keeping the first", alias)
            continue
        info = entry.get("model_info") or {}
        deployments[alias] = Deployment(
            alias=alias,
            model=_strip_provider_prefix(str(model)),
            extra_body=dict(params.get("extra_body") or {}),
            timeout_s=float(params.get("timeout") or DEFAULT_TIMEOUT_S),
            input_cost_per_token=info.get("input_cost_per_token"),
            output_cost_per_token=info.get("output_cost_per_token"),
        )

    fallbacks: dict[str, list[str]] = {}
    for rule in (raw.get("router_settings") or {}).get("fallbacks") or []:
        if isinstance(rule, dict):
            for src, targets in rule.items():
                if isinstance(targets, list):
                    fallbacks[src] = [str(t) for t in targets]

    st = p.stat()
    return Table(deployments=deployments, fallbacks=fallbacks,
                 loaded_at=time.time(), mtime=st.st_mtime, source=str(p))


class Registry:
    """Holds the current table and swaps it when the file changes."""

    def __init__(self, path: Path | None = None):
        self._path = Path(path or DEFAULT_CONFIG_PATH)
        self._lock = threading.Lock()
        self._table = load(self._path)
        logger.info("loaded %d deployments from %s", len(self._table.deployments), self._path)

    @property
    def table(self) -> Table:
        """Current table, reloading first if the file changed.

        A failed reload keeps the old table and logs. A config the operator
        broke must not take the router down with it -- the running one is known
        to work, and that is exactly the moment to keep using it.
        """
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._table
        if mtime == self._table.mtime:
            return self._table
        with self._lock:
            if mtime == self._table.mtime:      # another thread got there first
                return self._table
            try:
                fresh = load(self._path)
            except Exception as e:  # noqa: BLE001
                logger.error("config reload failed, keeping the running table: %s", e)
                # Remember the mtime anyway, or every request retries the parse.
                object.__setattr__(self._table, "mtime", mtime)
                return self._table
            logger.info("config reloaded: %d deployments", len(fresh.deployments))
            self._table = fresh
            return self._table
