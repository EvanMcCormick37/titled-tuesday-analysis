"""Adds per-market scoping to shared tables and creates the Bullet Brawl tables.

Idempotent, and each backend applies it in one transaction. Existing rows become
'titled_tuesday'. The column default also keeps the previous release's INSERTs
working, so this can run just before the new code deploys.
"""
from __future__ import annotations

import re

from .db import _ConnWrapper, is_postgres, table_columns

DEFAULT_MARKET = 'titled_tuesday'
_COLUMN = f"market TEXT NOT NULL DEFAULT '{DEFAULT_MARKET}'"

_ADD_COLUMN_ONLY = ('latest_model_predictions', 'latest_model_predictions_raw', 'manual_adjustments')

# table → (constraint kind, previous key columns)
_REKEY: dict[str, tuple[str, tuple[str, ...]]] = {
    'pipeline_runs': ('UNIQUE', ('tourn_date',)),
    'historical_predictions': ('PRIMARY KEY', ('tourn_date', 'username')),
    'backtest': ('PRIMARY KEY', ('tourn_date', 'model', 'username')),
    'attendance_conflicts': ('PRIMARY KEY', ('date', 'username')),
}

BULLET_BRAWL_DDL = (
    """
    CREATE TABLE IF NOT EXISTS bullet_brawl_tournaments (
        tournament_slug TEXT PRIMARY KEY,
        date            TIMESTAMPTZ,
        start_ts        BIGINT,
        finish_ts       BIGINT,
        num_players     INTEGER,
        num_games       INTEGER,
        winner          TEXT,
        url             TEXT,
        ingested_at     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bullet_brawl_standings (
        tournament_slug TEXT NOT NULL,
        date            TIMESTAMPTZ,
        username        TEXT NOT NULL,
        display_name    TEXT,
        rank            REAL,
        score           REAL,
        tie_break       REAL,
        wins            REAL,
        draws           REAL,
        losses          REAL,
        games           INTEGER,
        longest_streak  INTEGER,
        avg_rating      REAL,
        first_game_ts   BIGINT,
        last_game_ts    BIGINT,
        PRIMARY KEY (tournament_slug, username)
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_bbs_user ON bullet_brawl_standings(username)',
    'CREATE INDEX IF NOT EXISTS idx_bbs_date ON bullet_brawl_standings(date)',
)


def _exists(conn: _ConnWrapper, table: str) -> bool:
    return bool(table_columns(conn, table))


def _rekey_postgres(conn: _ConnWrapper, table: str, kind: str, old_key: tuple[str, ...]) -> None:
    conn.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN}')
    rows = conn.execute(
        """
        SELECT c.conname, array_agg(a.attname::text ORDER BY k.ord)
          FROM pg_constraint c
         CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE c.conrelid = ?::regclass AND c.contype IN ('p', 'u')
         GROUP BY c.conname
        """,
        (table,),
    ).fetchall()
    keys = {name: tuple(cols) for name, cols in rows}
    new_key = ('market', *old_key)
    if new_key in keys.values():
        return
    for name, cols in keys.items():
        if cols == old_key:
            conn.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
    suffix = 'pkey' if kind == 'PRIMARY KEY' else 'market_key'
    conn.execute(f'ALTER TABLE {table} ADD CONSTRAINT {table}_{suffix} {kind} ({", ".join(new_key)})')


def _rekey_sqlite(conn: _ConnWrapper, table: str, kind: str, old_key: tuple[str, ...]) -> None:
    """SQLite can't alter constraints, so copy into a table with the new key."""
    columns = conn.execute(f'PRAGMA table_info({table})').fetchall()
    names = [c[1] for c in columns]
    if 'market' in names:
        return
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    body = sql[sql.index('(') + 1:sql.rindex(')')]
    body = re.sub(r',\s*(?:PRIMARY\s+KEY|UNIQUE)\s*\([^)]*\)', '', body, flags=re.I)
    if len(old_key) == 1:
        body = re.sub(rf'("?{old_key[0]}"?[^,]*?)\s+UNIQUE\b', r'\1', body, count=1, flags=re.I)

    temp = f'{table}__multi_market'
    new_key = ', '.join(('market', *old_key))
    quoted = ', '.join(f'"{n}"' for n in names)
    conn.execute(f'CREATE TABLE {temp} ({body.rstrip()}, {_COLUMN}, {kind} ({new_key}))')
    conn.execute(f'INSERT INTO {temp} ({quoted}) SELECT {quoted} FROM {table}')
    conn.execute(f'DROP TABLE {table}')
    conn.execute(f'ALTER TABLE {temp} RENAME TO {table}')


def _add_column(conn: _ConnWrapper, table: str) -> None:
    if 'market' not in table_columns(conn, table):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {_COLUMN}')


def migrate(conn: _ConnWrapper) -> list[str]:
    """Bring `conn`'s database to the multi-market shape. The caller commits."""
    if not is_postgres():
        conn.execute('BEGIN')
    changed: list[str] = []

    for table in _ADD_COLUMN_ONLY:
        if _exists(conn, table):
            _add_column(conn, table)
            changed.append(table)

    rekey = _rekey_postgres if is_postgres() else _rekey_sqlite
    for table, (kind, old_key) in _REKEY.items():
        if _exists(conn, table):
            rekey(conn, table, kind, old_key)
            changed.append(table)

    if _exists(conn, 'attendance_conflicts'):
        conn.execute('CREATE INDEX IF NOT EXISTS idx_attendance_conflicts_username '
                     'ON attendance_conflicts(username)')
    if _exists(conn, 'manual_adjustments'):
        conn.execute('CREATE INDEX IF NOT EXISTS idx_adj_market ON manual_adjustments(market, tourn_date)')

    for stmt in BULLET_BRAWL_DDL:
        conn.execute(stmt)
    changed.extend(('bullet_brawl_tournaments', 'bullet_brawl_standings'))
    return changed
