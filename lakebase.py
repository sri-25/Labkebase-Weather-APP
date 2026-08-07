"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL (a standard Postgres connection URL,
e.g. postgresql://role:password@host:5432/databricks_postgres?sslmode=require)
pointing at a native Postgres role with a static, non-expiring password.
This keeps setup to a single secret instead of several separate env vars.

Originally matched the instructor's canonical reference app's pattern
exactly (psycopg2 + RealDictCursor + SQLAlchemy) - see DECISIONS.md
"Phase 1.5". Switched to psycopg3 later (see DECISIONS.md "Phase 10 -
scheduled job") after psycopg2-binary crashed with SIGABRT on import
under Databricks serverless - its bundled OpenSSL collides with grpc's
in that runtime. psycopg3's binary wheel doesn't bundle OpenSSL the same
way, and the %s-style parameter placeholders used everywhere in this
project stayed unchanged, so this was a driver swap, not a query rewrite.
"""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager

import psycopg
from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine

_w = WorkspaceClient()

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "weather")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _lakebase_url() -> str:
    """Fetch and decode the Lakebase connection URL from the Databricks secret scope."""
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg connection with dict_row as the row factory
    (rows come back as dicts, e.g. row["headline"], instead of positional
    tuples - same shape psycopg2's RealDictCursor gave us)."""
    conn = psycopg.connect(_lakebase_url(), row_factory=psycopg.rows.dict_row)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase (handy for pandas.read_sql
    or other tools that expect a SQLAlchemy-style connection). SQLAlchemy
    needs the postgresql+psycopg:// scheme to pick the psycopg3 driver
    instead of defaulting to (the no-longer-installed) psycopg2."""
    url = _lakebase_url()
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return create_engine(url)


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE/DDL statement against Lakebase, return
    affected row count (0 for DDL like CREATE TABLE)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
