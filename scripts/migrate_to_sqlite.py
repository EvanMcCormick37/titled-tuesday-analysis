#!/usr/bin/env python3
"""
One-time migration: CSV data files -> SQLite database at data/titled_tuesday.db.

Tables created
--------------
  standings          from data/titled_tuesday_standings.csv
  tournaments        from data/titled_tuesday_tournaments.csv
  username_to_player from data/username_to_player.csv
  kalshi_portfolio   from data/Kalshi-Advanced-Portfolio.csv
  model_predictions  from data/model_predictions/latest.csv  (if present)

Usage
-----
    python scripts/migrate_to_sqlite.py
    python scripts/migrate_to_sqlite.py --overwrite   # drop and recreate all tables
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.config import DB_PATH, DATA_DIR


def migrate(overwrite: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)

    def load(name: str, path: Path, parse_dates=None):
        if not path.exists():
            print(f'  SKIP {name}: {path} not found')
            return
        if overwrite:
            conn.execute(f'DROP TABLE IF EXISTS {name}')
        df = pd.read_csv(path, parse_dates=parse_dates or [])
        df.to_sql(name, conn, if_exists='replace' if overwrite else 'fail', index=False)
        print(f'  {name}: {len(df):,} rows')

    print(f'Migrating CSVs -> {DB_PATH}\n')

    # standings
    load('standings', DATA_DIR / 'titled_tuesday_standings.csv', parse_dates=['date'])
    conn.execute('CREATE INDEX IF NOT EXISTS idx_standings_date     ON standings(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_standings_slug     ON standings(tournament_slug)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_standings_username ON standings(username)')

    # tournaments
    load('tournaments', DATA_DIR / 'titled_tuesday_tournaments.csv', parse_dates=['date'])
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tournaments_date ON tournaments(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tournaments_slug ON tournaments(slug)')

    # username_to_player (headerless CSV: username,player_name)
    utp_path = DATA_DIR / 'username_to_player.csv'
    if utp_path.exists():
        if overwrite:
            conn.execute('DROP TABLE IF EXISTS username_to_player')
        conn.execute(
            'CREATE TABLE IF NOT EXISTS username_to_player '
            '(username TEXT PRIMARY KEY, player_name TEXT)'
        )
        with open(utp_path, newline='', encoding='utf-8') as f:
            rows = list(csv.reader(f))
        conn.executemany('INSERT OR REPLACE INTO username_to_player VALUES (?, ?)', rows)
        print(f'  username_to_player: {len(rows)} rows')

    # Kalshi portfolio snapshot
    load('kalshi_portfolio', DATA_DIR / 'Kalshi-Advanced-Portfolio.csv')

    # Latest model predictions (optional)
    pred_path = DATA_DIR / 'model_predictions' / 'latest.csv'
    if pred_path.exists():
        load('model_predictions', pred_path)

    conn.commit()
    conn.close()
    print(f'\nDone: {DB_PATH}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--overwrite', action='store_true',
                        help='Drop and recreate all tables')
    args = parser.parse_args()
    migrate(overwrite=args.overwrite)
