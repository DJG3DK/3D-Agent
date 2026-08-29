"""Shared infra reused by the current graph (agent/outer_graph.py).
project_lock/open_checkpointer/open_store are generic (parameterized by
config, not tied to any particular state schema), and outer_graph.py
re-exports them from here rather than duplicating them.
"""

import asyncio
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from agent.config import Config

# A pool, not a single long-lived connection: a bare connection held open for
# this process's entire lifetime (it runs for days) goes stale silently
# across any Postgres restart, and psycopg does not notice until the next
# query fails. The pool's `check` runs a liveness probe on every checkout,
# right before a caller actually uses the connection, discarding and
# replacing anything that fails it; `max_idle`/`max_lifetime` recycle
# connections proactively in the background.
#
# `langgraph.checkpoint.postgres._ainternal.Conn` accepts either a bare
# connection or a pool, so both Saver and Store take a pool as `conn`
# transparently. AsyncPostgresStore.from_conn_string supports a pool via its
# `pool_config` kwarg; AsyncPostgresSaver.from_conn_string only opens a
# single bare connection, so the checkpointer builds and owns its pool
# explicitly here instead.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 10
_POOL_MAX_IDLE = 300      # seconds a connection may sit unused before recycling
_POOL_MAX_LIFETIME = 1800  # seconds before a connection is recycled regardless
_POOL_KWARGS = {"autocommit": True, "prepare_threshold": 0}


def _make_pool(config: Config) -> AsyncConnectionPool:
    from psycopg.rows import dict_row

    return AsyncConnectionPool(
        config.pg_dsn,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        kwargs={**_POOL_KWARGS, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
        max_idle=_POOL_MAX_IDLE,
        max_lifetime=_POOL_MAX_LIFETIME,
        open=False,
    )

# One task at a time per project. Executing two tasks against the same
# sandbox directory concurrently would corrupt each other's uncommitted
# work; this is an in-process guard against that rather than relying on
# callers to serialize requests correctly.
_project_locks: dict[str, asyncio.Lock] = {}


def project_lock(repo: str) -> asyncio.Lock:
    if repo not in _project_locks:
        _project_locks[repo] = asyncio.Lock()
    return _project_locks[repo]


@asynccontextmanager
async def open_checkpointer(config: Config):
    pool = _make_pool(config)
    await pool.open(wait=True)
    try:
        saver = AsyncPostgresSaver(conn=pool)
        # setup() must be called before first use of a Postgres
        # checkpointer/store -- it creates tables and runs migrations. It
        # checks the currently applied migration version and only runs
        # newer ones, so calling it on every startup is safe and cheap.
        # Despite being on the async saver class, this method is genuinely
        # named `setup()`, not `asetup()`.
        await saver.setup()
        yield saver
    finally:
        await pool.close()


@asynccontextmanager
async def open_store(config: Config):
    pool_config = {
        "min_size": _POOL_MIN_SIZE,
        "max_size": _POOL_MAX_SIZE,
        "max_idle": _POOL_MAX_IDLE,
        "max_lifetime": _POOL_MAX_LIFETIME,
        "check": AsyncConnectionPool.check_connection,
        "kwargs": _POOL_KWARGS,
    }
    async with AsyncPostgresStore.from_conn_string(
        config.pg_dsn, pool_config=pool_config
    ) as store:
        # AsyncPostgresStore's async setup method is also just named
        # `setup()`, not `asetup()`. Same reasoning as open_checkpointer above.
        await store.setup()
        yield store
