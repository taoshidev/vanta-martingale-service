"""Shared fixtures.

DB-backed tests need ``TEST_DATABASE_URL`` pointing at a THROWAWAY local Postgres database;
on every test they DROP and re-create this service's tables/types there from ``schema.sql``.
Without the variable those tests skip and the pure-logic suite still runs. Example:

    createdb martingale_test
    TEST_DATABASE_URL=postgresql://localhost/martingale_test pytest
"""
import os
import pathlib

import pytest

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

_DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS martingale_order_evaluations CASCADE",
    "DROP TABLE IF EXISTS martingale_subaccount_state CASCADE",
    "DROP TYPE IF EXISTS martingale_subaccount_status CASCADE",
    "DROP TYPE IF EXISTS martingale_elimination_action CASCADE",
    "DROP TYPE IF EXISTS martingale_trigger_outcome CASCADE",
)


def _statements(sql):
    """Yield each statement from schema.sql. Strips ``--`` line comments first (they may contain
    semicolons), then splits on ';'. schema.sql has no string literals, so this is safe."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    for chunk in no_comments.split(";"):
        if chunk.strip():
            yield chunk


@pytest.fixture()
def store():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set (DB-backed test)")
    psycopg = pytest.importorskip("psycopg")
    from martingale_service.db import Store

    with psycopg.connect(dsn, autocommit=True) as conn:
        for stmt in _DROP_STATEMENTS:
            conn.execute(stmt)
        for stmt in _statements(SCHEMA_PATH.read_text()):
            conn.execute(stmt)

    s = Store(dsn)
    yield s
    s.close()
