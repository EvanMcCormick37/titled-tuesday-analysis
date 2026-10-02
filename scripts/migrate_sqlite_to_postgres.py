#!/usr/bin/env python3
"""One-shot SQLite -> Postgres data migration for the Phase 1 cutover.

For each known table:
  1. Read every row from SQLite (data/titled_tuesday.db).
  2. Bulk-insert into Postgres via psycopg's executemany.
  3. Verify row counts match post-load.

Uses INSERT ... ON CONFLICT DO NOTHING so the script is idempotent — re-running
is safe and skips already-copied rows based on each table's PK/UNIQUE
constraints. (A `--truncate` flag wipes the target tables first for a clean
redo.)

Mixed-case columns (P_topN_given_play, marketTicker, etc.) are correctly quoted
on both sides.

Reads DATABASE_URL from env (via src.db).

Usage:
    python scripts/migrate_sqlite_to_postgres.py              # idempotent copy
    python scripts/migrate_sqlite_to_postgres.py --truncate   # wipe PG tables first
    python scripts/migrate_sqlite_to_postgres.py --dry-run    # row counts only
"""
import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH
from src.db import get_conn, is_postgres


# ── Table manifest ───────────────────────────────────────────────────────────
# Each entry: (table_name, conflict_target or None).  conflict_target is the
# column list that defines a duplicate row; used for ON CONFLICT DO NOTHING.
# None means no uniqueness → always append (acceptable only for tables we
# TRUNCATE first).
MANIFEST = [
    ('player_information',          'username'),
    ('titled_tuesday_tournaments',  'tournament_slug'),
    ('titled_tuesday_standings',     None),             # no natural PK; truncate+append on each full run
    ('attendance_conflicts',        '(date, username)'),
    ('other_events',                'broadcast_name'),
    ('other_event_rounds',          '(broadcast_name, round)'),
    ('other_event_participants',    '(broadcast_name, player_name)'),
    ('latest_model_predictions',     None),             # replace-mode table
    ('latest_model_predictions_raw', None),
    ('historical_predictions',      '(tourn_date, username)'),
    ('backtest',                    '(tourn_date, model, username)'),
    ('kalshi_markets',              'market_ticker'),
    ('kalshi_market_snapshots',      None),             # append-only; UNIQUE index drops dups where present
    ('kalshi_portfolio',             None),
    ('manual_adjustments',           None),             # BIGSERIAL id; append-only
    ('pipeline_runs',               'tourn_date'),
    ('job_runs',                     None),             # BIGSERIAL id; append-only
]

# Columns that are case-sensitive in PG — need double-quoting in generated SQL.
MIXED_CASE_COLS = {
    'P_top1_given_play', 'P_top3_given_play', 'P_top5_given_play',
    'P_top8_given_play', 'P_top10_given_play',
    'marketTicker', 'eventTitle', 'marketTitle', 'closeDate',
    'yesPrice', 'noPrice', 'lastTradePrice', 'averagePrice',
    'positionReturns', 'lockedInReturns', 'totalReturns',
}


def _quoted(col: str) -> str:
    return f'"{col}"' if col in MIXED_CASE_COLS else col


def _sqlite_columns(sconn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in sconn.execute(f'PRAGMA table_info({table})').fetchall()]


def _count(conn, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def _migrate_one(
    sconn: sqlite3.Connection,
    pconn,
    table: str,
    conflict_target: str | None,
    truncate: bool,
    dry_run: bool,
) -> tuple[int, int]:
    sqlite_n = _count(sconn, table)
    pg_before = _count(pconn, table)

    if dry_run:
        return sqlite_n, pg_before

    if truncate and pg_before > 0:
        pconn.execute(f'TRUNCATE TABLE {table} RESTART IDENTITY CASCADE')
        pconn.commit()
        pg_before = 0

    if sqlite_n == 0:
        return 0, pg_before

    cols = _sqlite_columns(sconn, table)
    cols_sql = ', '.join(_quoted(c) for c in cols)
    place_sql = ', '.join(['%s'] * len(cols))

    if conflict_target:
        sql = (f'INSERT INTO {table} ({cols_sql}) VALUES ({place_sql}) '
               f'ON CONFLICT {conflict_target if conflict_target.startswith("(") else f"({conflict_target})"} '
               f'DO NOTHING')
    else:
        sql = f'INSERT INTO {table} ({cols_sql}) VALUES ({place_sql})'

    # Stream in chunks so we don't balloon memory for the big standings table.
    chunk_size = 5_000
    cursor = sconn.execute(f'SELECT {", ".join(cols)} FROM {table}')
    with pconn.cursor() as pcur:
        batch = []
        for row in cursor:
            batch.append(tuple(row))
            if len(batch) >= chunk_size:
                pcur.executemany(sql, batch)
                batch.clear()
        if batch:
            pcur.executemany(sql, batch)
    pconn.commit()
    pg_after = _count(pconn, table)
    return sqlite_n, pg_after


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--no-truncate', action='store_true',
                    help='Skip the TRUNCATE step and append on top of existing PG data '
                         '(uses ON CONFLICT DO NOTHING where applicable). Default is to '
                         'truncate for a clean one-shot cutover.')
    ap.add_argument('--dry-run', action='store_true',
                    help='Report row counts only; no writes.')
    args = ap.parse_args()
    truncate = not args.no_truncate

    if not is_postgres():
        raise RuntimeError('DATABASE_URL must point at Postgres to run this migration.')

    sconn = sqlite3.connect(DB_PATH)
    pconn = get_conn().raw   # psycopg3 connection (no translation needed — we build raw PG SQL here)

    try:
        print(f'{"Table":32s} {"sqlite":>10s} {"pg_after":>10s} {"delta":>10s}')
        print('-' * 65)
        totals = {'sqlite': 0, 'pg_after': 0}
        for table, conflict_target in MANIFEST:
            s_n, p_n = _migrate_one(sconn, pconn, table, conflict_target,
                                    truncate=truncate, dry_run=args.dry_run)
            totals['sqlite'] += s_n
            totals['pg_after'] += p_n
            delta = p_n - s_n
            marker = '' if delta == 0 or args.dry_run else (' !!' if delta < 0 else ' +')
            print(f'{table:32s} {s_n:>10,} {p_n:>10,} {delta:>+10,}{marker}', flush=True)
        print('-' * 65)
        print(f'{"TOTAL":32s} {totals["sqlite"]:>10,} {totals["pg_after"]:>10,}')
        if args.dry_run:
            print('\n(dry run — no data was written to Postgres)')
            return

        # Reset BIGSERIAL / SERIAL sequences on tables whose id column was
        # bulk-loaded from SQLite. Otherwise the next INSERT via the sequence
        # would start at 1 and collide with the migrated rows.
        print('\nResetting serial sequences...')
        for table in ('manual_adjustments', 'pipeline_runs', 'job_runs'):
            with pconn.cursor() as cur:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    f"       COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)",
                    (table,),
                )
                new_val = cur.fetchone()[0]
            print(f'  {table}: sequence now at {new_val}')
        pconn.commit()
    finally:
        sconn.close()
        pconn.close()


if __name__ == '__main__':
    main()
