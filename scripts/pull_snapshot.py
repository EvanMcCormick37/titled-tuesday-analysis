#!/usr/bin/env python3
"""Copy the Railway Postgres database into data/chess_tournament_data.db for local analysis.

The connection to Railway is read-only. The local file is replaced only after every
table has been copied and its row count checked; the previous file is kept as
data/chess_tournament_data.db.bak. Backtest history lives in data/backtests.db and is not
touched.

Use the public URL from the Railway UI (Postgres service, Variables,
DATABASE_PUBLIC_URL). Paste it at the prompt, or set SNAPSHOT_DATABASE_URL. Do not
put it in .env.local: src/db.py loads that file, and everything local would then
run against production.

Usage:
    python scripts/pull_snapshot.py
"""
import argparse
import getpass
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from sqlalchemy import create_engine, text

from src import db
from src.config import DB_PATH
from src.multi_market_schema import migrate

# Operational state with no analytical use.
_SKIPPED_TABLES = {'trade_jobs', 'trade_job_events', 'market_snapshots'}
_CHUNK_ROWS = 50_000


def _source_engine(url: str):
    if not url.startswith(('postgres://', 'postgresql://')):
        raise ValueError('Expected a postgres:// or postgresql:// URL')
    sa_url = 'postgresql+psycopg://' + url.split('://', 1)[1]
    return create_engine(sa_url, connect_args={'options': '-c default_transaction_read_only=on'})


def _sqlite_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
            return value.strftime('%Y-%m-%d %H:%M:%S+00:00')
        return value.isoformat(sep=' ')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _for_sqlite(frame: pd.DataFrame) -> pd.DataFrame:
    """Store timestamps as the same text the local scraper writes, and JSON as text."""
    out = frame.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_convert('UTC').dt.strftime('%Y-%m-%d %H:%M:%S+00:00')
        elif out[col].dtype == object:
            out[col] = out[col].map(_sqlite_value)
    return out


def _unique_keys(conn, table: str) -> list[list[str]]:
    rows = conn.execute(text("""
        SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM pg_constraint c
         CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE c.conrelid = CAST(:table AS regclass) AND c.contype IN ('p', 'u')
         GROUP BY c.conname
    """), {'table': f'public."{table}"'}).fetchall()
    return [list(r[0]) for r in rows]


def _quoted(names: list[str]) -> str:
    return ', '.join(f'"{n}"' for n in names)


def copy_tables(url: str, dest: Path) -> dict[str, int]:
    """Copy every analytical table into a new SQLite file at `dest`. Returns row counts."""
    counts: dict[str, int] = {}
    engine = _source_engine(url)
    target = sqlite3.connect(dest)
    try:
        # One repeatable-read transaction, so all tables come from the same moment.
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as source:
            tables = [r[0] for r in source.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )) if r[0] not in _SKIPPED_TABLES]
            for table in tables:
                expected = source.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
                keys = _unique_keys(source, table)
                copied = 0
                streaming = source.execution_options(stream_results=True)
                for chunk in pd.read_sql_query(text(f'SELECT * FROM "{table}"'), streaming,
                                               chunksize=_CHUNK_ROWS):
                    _for_sqlite(chunk).to_sql(table, target, if_exists='append', index=False)
                    copied += len(chunk)
                if copied != expected:
                    raise RuntimeError(f'{table}: copied {copied:,} rows, expected {expected:,}')
                for i, key in enumerate(keys):
                    target.execute(f'CREATE UNIQUE INDEX "uq_{table}_{i}" ON "{table}" ({_quoted(key)})')
                counts[table] = copied
                print(f'  {table:<34} {copied:>10,}')
        target.commit()
    finally:
        target.close()
        engine.dispose()
    return counts


def apply_local_schema(path: Path) -> None:
    """Bring the copy to the current schema, whether or not Railway has been migrated."""
    original = db.DB_PATH
    db.DB_PATH = path
    try:
        with db.get_conn() as conn:
            migrate(conn)
    finally:
        db.DB_PATH = original


def pull(url: str, dest: Path) -> dict[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix('.partial')
    partial.unlink(missing_ok=True)
    try:
        counts = copy_tables(url, partial)
        apply_local_schema(partial)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if dest.exists():
        os.replace(dest, dest.with_suffix('.db.bak'))
    os.replace(partial, dest)
    return counts


def main() -> None:
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    # src.db may have loaded .env.local; the local steps below must stay on SQLite.
    os.environ.pop('DATABASE_URL', None)
    url = os.environ.get('SNAPSHOT_DATABASE_URL') or getpass.getpass('Railway DATABASE_PUBLIC_URL: ')
    print(f'Copying Railway into {DB_PATH} ...')
    counts = pull(url.strip(), Path(DB_PATH))
    print(f'Done: {len(counts)} tables, {sum(counts.values()):,} rows.')


if __name__ == '__main__':
    main()
