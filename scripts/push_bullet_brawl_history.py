#!/usr/bin/env python3
"""Copy the locally backfilled Bullet Brawl history to Railway. Run once, before the
first Bullet Brawl pipeline run there.

Copies, from data/chess_tournament_data.db:
  - bullet_brawl_tournaments and bullet_brawl_standings (each event replaces its
    own rows on Railway, so a rerun is safe)
  - player_information rows for Bullet Brawl accounts Railway doesn't have yet.
    Existing Railway rows are never changed.

Enrich names locally first so they come along:
    python scripts/scraping/update_bullet_brawl.py --enrich-only     (with DATABASE_URL unset)

Railway needs the multi-market schema (scripts/migrate_multi_market.py); this
script applies it if the tables are missing. Paste the Postgres public URL at the
prompt, or set PUSH_DATABASE_URL. Do not put it in .env.local.

Usage:
    python scripts/push_bullet_brawl_history.py
    python scripts/push_bullet_brawl_history.py --dry-run
"""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psycopg

from src.bullet_brawl_ingest import STANDINGS_COLUMNS
from src.config import DB_PATH

TOURNAMENT_COLUMNS = ('tournament_slug', 'date', 'start_ts', 'finish_ts', 'num_players', 'num_games',
                      'winner', 'url', 'ingested_at')
BATCH = 5_000


def _local_rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[tuple]:
    return conn.execute(f'SELECT {", ".join(columns)} FROM {table}').fetchall()


def _ensure_schema(url: str) -> None:
    with psycopg.connect(url) as pg:
        exists = pg.execute("SELECT to_regclass('bullet_brawl_standings')").fetchone()[0]
    if exists is None:
        print('Bullet Brawl tables missing on Railway; applying the multi-market schema...')
        os.environ['DATABASE_URL'] = url
        from src.db import get_conn
        from src.multi_market_schema import migrate
        with get_conn() as conn:
            migrate(conn)


def push(url: str, dry_run: bool = False) -> dict[str, int]:
    local = sqlite3.connect(DB_PATH)
    try:
        tournaments = _local_rows(local, 'bullet_brawl_tournaments', TOURNAMENT_COLUMNS)
        standings = _local_rows(local, 'bullet_brawl_standings', STANDINGS_COLUMNS)
        info_cols = [r[1] for r in local.execute('PRAGMA table_info(player_information)').fetchall()]
        bb_users = {r[0] for r in local.execute('SELECT DISTINCT username FROM bullet_brawl_standings')}
        info = [r for r in local.execute(f'SELECT {", ".join(info_cols)} FROM player_information')
                if r[0] in bb_users]
    finally:
        local.close()
    print(f'Local: {len(tournaments)} events, {len(standings):,} standings rows, '
          f'{len(info):,} player_information rows for Bullet Brawl accounts')
    if not tournaments:
        raise SystemExit('No local Bullet Brawl events. Run scripts/scraping/backfill_bullet_brawl.py first.')

    if not dry_run:
        _ensure_schema(url)
    with psycopg.connect(url) as pg:
        remote_cols = {r[0] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'player_information'")}
        cols = [c for c in info_cols if c in remote_cols]
        remote_users = {r[0] for r in pg.execute('SELECT username FROM player_information')}
        new_info = [tuple(row[info_cols.index(c)] for c in cols) for row in info if row[0] not in remote_users]
        print(f'Railway: {len(new_info):,} of those accounts are new')
        if dry_run:
            print('Dry run: nothing written.')
            return {'events': len(tournaments), 'standings': len(standings), 'players': len(new_info)}

        slugs = [(t[0],) for t in tournaments]
        with pg.transaction():
            with pg.cursor() as cur:
                cur.executemany('DELETE FROM bullet_brawl_standings WHERE tournament_slug = %s', slugs)
                cur.executemany('DELETE FROM bullet_brawl_tournaments WHERE tournament_slug = %s', slugs)
                cur.executemany(
                    f'INSERT INTO bullet_brawl_tournaments ({", ".join(TOURNAMENT_COLUMNS)}) '
                    f'VALUES ({", ".join(["%s"] * len(TOURNAMENT_COLUMNS))})', tournaments)
                for i in range(0, len(standings), BATCH):
                    cur.executemany(
                        f'INSERT INTO bullet_brawl_standings ({", ".join(STANDINGS_COLUMNS)}) '
                        f'VALUES ({", ".join(["%s"] * len(STANDINGS_COLUMNS))})', standings[i:i + BATCH])
                    print(f'  standings {min(i + BATCH, len(standings)):,}/{len(standings):,}')
                if new_info:
                    cur.executemany(
                        f'INSERT INTO player_information ({", ".join(cols)}) '
                        f'VALUES ({", ".join(["%s"] * len(cols))}) ON CONFLICT DO NOTHING', new_info)
    return {'events': len(tournaments), 'standings': len(standings), 'players': len(new_info)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='count what would be copied, write nothing')
    args = ap.parse_args()
    os.environ.pop('DATABASE_URL', None)    # the local side must stay on SQLite
    url = os.environ.get('PUSH_DATABASE_URL') or getpass.getpass('Railway DATABASE_PUBLIC_URL: ')
    url = url.strip().replace('postgres://', 'postgresql://', 1)
    print(push(url, args.dry_run))


if __name__ == '__main__':
    main()
