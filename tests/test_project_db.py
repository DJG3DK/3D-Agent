"""The read-only project-database tool (agent/tools/project_db.py).

The agent had no way to see application DATA -- only code. Asked to work on
a specific product, it searched the codebase, seeds, migrations and full git
history, correctly found nothing, and stopped to ask the operator: the
product is a row in Postgres, not a thing in the repo.

These tests pin the safety envelope. The DSN-dependent paths are covered by
live use; what's asserted here is the logic that must hold with no database
present -- statement filtering, ORM-param stripping, and per-project
opt-in.
"""

import pytest

from agent.tools.project_db import (
    _ORM_ONLY_PARAMS,
    _READ_ONLY_START,
    _dsn_for,
    _render,
    _strip_orm_params,
    make_project_db_tool,
)


# --- statement filtering ----------------------------------------------------
# First layer only. The real guarantee is the READ ONLY transaction, which
# catches what this can't -- e.g. `WITH d AS (DELETE ... RETURNING id)`,
# which starts with WITH and so passes this check but is rejected outright
# by Postgres (confirmed live).

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select id from foo",
    "WITH p AS (SELECT 1) SELECT * FROM p",
    "\n\tSELECT * FROM bar",
])
def test_reads_are_accepted(sql):
    assert _READ_ONLY_START.match(sql)


@pytest.mark.parametrize("sql", [
    "UPDATE product SET title='x'",
    "DELETE FROM product",
    "DROP TABLE product",
    "INSERT INTO product VALUES (1)",
    "TRUNCATE product",
    "ALTER TABLE product ADD COLUMN x int",
    "GRANT ALL ON product TO someone",
    "COPY product TO '/tmp/x'",
])
def test_writes_and_ddl_are_rejected(sql):
    assert not _READ_ONLY_START.match(sql)


# --- ORM DSN compatibility --------------------------------------------------

def test_prisma_schema_param_is_stripped():
    """libpq rejects the whole URI on an unknown query param, so a perfectly
    valid Prisma DATABASE_URL was unusable purely because of `?schema=public`
    (hit live on the first real query)."""
    dsn = _strip_orm_params("postgresql://u:p@localhost:5432/db?schema=public")
    assert dsn == "postgresql://u:p@localhost:5432/db"


def test_real_libpq_params_survive_stripping():
    dsn = _strip_orm_params("postgresql://u:p@h:5432/db?sslmode=require&schema=public")
    assert "sslmode=require" in dsn
    assert "schema" not in dsn


def test_dsn_without_params_is_untouched():
    raw = "postgresql://u:p@localhost:5432/db"
    assert _strip_orm_params(raw) == raw


def test_every_stripped_param_is_orm_only():
    """Guards against someone adding a genuine libpq param to the strip list
    and silently breaking TLS or timeouts."""
    for real in ("sslmode", "connect_timeout", "application_name", "host", "port"):
        assert real not in _ORM_ONLY_PARAMS


# --- per-project opt-in -----------------------------------------------------

def test_project_without_db_config_gets_no_tool(monkeypatch):
    """A project with no database configured shouldn't be handed a tool that
    always errors -- it simply isn't offered one."""
    import agent.tools.project_db as mod
    monkeypatch.setitem(mod.PROJECTS, "no-db-project", {"sandbox": "/tmp/a", "live": "/tmp/b"})
    assert _dsn_for("no-db-project") is None
    assert make_project_db_tool("no-db-project") is None


def test_unknown_project_is_handled(monkeypatch):
    assert _dsn_for("does-not-exist") is None
    assert make_project_db_tool("does-not-exist") is None


def test_missing_env_file_does_not_raise(monkeypatch):
    import agent.tools.project_db as mod
    monkeypatch.setitem(mod.PROJECTS, "broken", {"live": "/nonexistent", "db_env_file": "apps/api/.env"})
    assert _dsn_for("broken") is None


# --- output rendering -------------------------------------------------------

def test_empty_result_is_stated_not_blank():
    assert _render(["id"], []) == "(0 rows)"


def test_wide_cells_are_truncated_with_a_marker():
    out = _render(["blob"], [("x" * 5000,)])
    assert "…(+" in out
    assert len(out) < 1000


def test_newlines_in_cells_do_not_break_row_alignment():
    out = _render(["a", "b"], [("one\ntwo", "three")])
    assert out.count("\n") == 1  # header + exactly one data row
