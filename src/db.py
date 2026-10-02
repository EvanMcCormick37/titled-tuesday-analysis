"""Database connectivity — dual-backend (SQLite local, Postgres on Railway).

Call sites use the same two entry points regardless of backend:

    from src.db import get_conn, get_engine

    # DB-API style (for executemany / explicit SQL):
    with get_conn() as conn:
        conn.execute('SELECT ... WHERE id = ?', (id_,))

    # SQLAlchemy style (for pandas read_sql_query / to_sql):
    engine = get_engine()
    df = pd.read_sql_query('SELECT * FROM foo', engine)

Backend is picked at import time based on $DATABASE_URL:
  - $DATABASE_URL starts with 'postgres' → Postgres via psycopg3
  - otherwise                             → SQLite at config.DB_PATH

Query compatibility:
  - Positional `?` placeholders are auto-translated to `%s` on Postgres.
  - Keep SQL otherwise ANSI-standard; both backends tolerate the obvious
    overlap (SELECT, INSERT, UPDATE, DELETE, CREATE TABLE IF NOT EXISTS, ...).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable
from pathlib import Path

from .config import DB_PATH

# Load .env.local for local dev — gitignored, contains DATABASE_URL for Railway tests.
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parent.parent / '.env.local'
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass


def _database_url() -> str | None:
    url = os.environ.get('DATABASE_URL', '').strip()
    if url.startswith(('postgres://', 'postgresql://')):
        return url
    return None


def is_postgres() -> bool:
    return _database_url() is not None


# ── DB-API wrapper ────────────────────────────────────────────────────────────

class _ConnWrapper:
    """Thin wrapper that forwards DB-API calls, translating `?` to `%s` on PG.

    Supports the subset of DB-API methods used across this project:
        .execute(sql, params) → cursor-like (iterable, .fetchone(), .fetchall())
        .executemany(sql, seq)
        .cursor()
        .commit()
        .close()
    Also usable as a context manager; auto-commits on clean exit.
    """
    __slots__ = ('_conn', '_is_pg')

    def __init__(self, raw_conn: Any, is_pg: bool):
        self._conn = raw_conn
        self._is_pg = is_pg

    def _translate(self, sql: str) -> str:
        return sql.replace('?', '%s') if self._is_pg else sql

    def execute(self, sql: str, params: Iterable[Any] = ()):
        return self._conn.execute(self._translate(sql), params)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]):
        # psycopg3's Connection has no executemany — it's cursor-only. SQLite's
        # Connection accepts both. Use a cursor explicitly for cross-backend parity.
        cur = self._conn.cursor()
        try:
            cur.executemany(self._translate(sql), list(seq))
        finally:
            if hasattr(cur, 'close'):
                cur.close()

    def cursor(self):
        return _CursorWrapper(self._conn.cursor(), self._is_pg)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self) -> Any:
        """Underlying driver connection — use when a lib wants a native DB-API conn."""
        return self._conn

    def __enter__(self) -> '_ConnWrapper':
        return self

    def __exit__(self, exc_type, exc_val, tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


class _CursorWrapper:
    __slots__ = ('_cur', '_is_pg')

    def __init__(self, raw_cur: Any, is_pg: bool):
        self._cur = raw_cur
        self._is_pg = is_pg

    def execute(self, sql: str, params: Iterable[Any] = ()):
        self._cur.execute(sql.replace('?', '%s') if self._is_pg else sql, params)
        return self

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]):
        self._cur.executemany(sql.replace('?', '%s') if self._is_pg else sql, list(seq))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()


# ── Public API ────────────────────────────────────────────────────────────────

def get_conn() -> _ConnWrapper:
    """Return a wrapped DB-API connection to the configured backend."""
    url = _database_url()
    if url is not None:
        import psycopg
        return _ConnWrapper(psycopg.connect(url), is_pg=True)
    return _ConnWrapper(sqlite3.connect(DB_PATH), is_pg=False)


_ENGINE = None


def get_engine():
    """Return a cached SQLAlchemy Engine for pandas-style access."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from sqlalchemy import create_engine
    url = _database_url()
    if url is not None:
        # psycopg3 driver
        sa_url = url.replace('postgres://', 'postgresql+psycopg://')
        sa_url = sa_url.replace('postgresql://', 'postgresql+psycopg://') \
            if not sa_url.startswith('postgresql+psycopg://') else sa_url
        _ENGINE = create_engine(sa_url, pool_pre_ping=True)
    else:
        _ENGINE = create_engine(f'sqlite:///{DB_PATH}')
    return _ENGINE


def backend_name() -> str:
    """'postgres' or 'sqlite' — for debug/log output."""
    return 'postgres' if is_postgres() else 'sqlite'


# ── Backend-portable helpers ─────────────────────────────────────────────────

def table_columns(conn: _ConnWrapper, table: str) -> set[str]:
    """Return the set of column names for `table`, on either backend.

    Replaces direct PRAGMA table_info() usage. Returns an empty set if the
    table doesn't exist.
    """
    if is_postgres():
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchall()
        return {r[0] for r in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def upsert_many(
    conn: _ConnWrapper,
    table: str,
    columns: list[str],
    rows: list[tuple],
    conflict_cols: list[str],
) -> None:
    """Backend-portable INSERT OR REPLACE / INSERT ON CONFLICT DO UPDATE.

    On SQLite: translates to `INSERT OR REPLACE INTO ...`.
    On Postgres: translates to `INSERT ... ON CONFLICT (<keys>) DO UPDATE SET <non-key cols>=EXCLUDED.<col>`.

    `conflict_cols` is only used on Postgres (SQLite relies on table UNIQUE/PK).
    """
    if not rows:
        return
    cols_str  = ', '.join(columns)
    place_str = ', '.join(['?'] * len(columns))
    if is_postgres():
        non_key = [c for c in columns if c not in conflict_cols]
        if non_key:
            update_str = ', '.join(f'{c} = EXCLUDED.{c}' for c in non_key)
            sql = (f'INSERT INTO {table} ({cols_str}) VALUES ({place_str}) '
                   f'ON CONFLICT ({", ".join(conflict_cols)}) DO UPDATE SET {update_str}')
        else:
            # All columns are keys — nothing to update, just skip on conflict.
            sql = (f'INSERT INTO {table} ({cols_str}) VALUES ({place_str}) '
                   f'ON CONFLICT ({", ".join(conflict_cols)}) DO NOTHING')
    else:
        sql = f'INSERT OR REPLACE INTO {table} ({cols_str}) VALUES ({place_str})'
    conn.executemany(sql, rows)


def write_df_replace(df, table: str) -> None:
    """Overwrite a table's contents with the given DataFrame.

    SQLite: `to_sql(if_exists='replace')` — drops and recreates the table
      from the DataFrame's inferred types (matches current behaviour).
    Postgres: TRUNCATE the table (preserving the DDL from the schema init
      script), then `to_sql(if_exists='append')` to repopulate.

    Only safe for tables where the schema init script already defined the
    canonical shape — i.e. the two predictions tables. Everything else
    should write via `to_sql(if_exists='append')` directly on the engine.
    """
    engine = get_engine()
    if is_postgres():
        from sqlalchemy import text
        with engine.begin() as sa_conn:
            sa_conn.execute(text(f'TRUNCATE TABLE {table}'))
        df.to_sql(table, engine, if_exists='append', index=False)
    else:
        df.to_sql(table, engine, if_exists='replace', index=False)
