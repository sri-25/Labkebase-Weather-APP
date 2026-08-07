"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL (a standard Postgres connection URL)
pointing at a native Postgres role with a static password, stored as a
Databricks secret rather than a local .env - keeps setup to one secret
instead of several env vars, and means no credential ever touches disk.

Uses psycopg3, not psycopg2 - psycopg2-binary's bundled OpenSSL collides
with grpc's under Databricks serverless (SIGABRT on import). See
DECISIONS.md for the full crash writeup if this ever needs revisiting.
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
