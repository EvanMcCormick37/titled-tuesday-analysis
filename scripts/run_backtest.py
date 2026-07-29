#!/usr/bin/env python3
"""
Walk-forward backtest: score/tiebreak Monte Carlo model.

For each Titled Tuesday from START_DATE onward, restricts training data to
everything before that date and runs the same simulation pipeline used in
make_predictions.py.  No schedule adjustments are applied (those require
per-week human input and are stored separately in historical_predictions).

Predictions are written to backtest_predictions in data/titled_tuesday.db.
Actual outcomes can be inferred by joining with titled_tuesday_standings on
(tourn_date = date(date), username), or with kalshi_markets on player_name.

Usage
-----
    python scripts/run_backtest.py
    python scripts/run_backtest.py --start 2025-11-01
    python scripts/run_backtest.py --sims 10000
    python scripts/run_backtest.py --sample 5    # quick test, first 5 dates
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.config import DB_PATH, N_SIMS
from src.data import load_and_prepare
from src.simulation import build_player_pool_score, run_simulation_score, build_results

START_DATE = '2025-11-01'
N_VALUES   = [1, 3, 5, 8, 10]
BASE_SEED  = 42


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_predictions (
            tourn_date         TEXT,
            username           TEXT,
            p_participate      REAL,
            P_top1_given_play  REAL,
            P_top3_given_play  REAL,
            P_top5_given_play  REAL,
            P_top8_given_play  REAL,
            P_top10_given_play REAL,
            PRIMARY KEY (tourn_date, username)
        )
    """)
    conn.commit()


def run_backtest(
    start_date: str = START_DATE,
    n_sims: int     = N_SIMS,
    n_values: list  = N_VALUES,
    base_seed: int  = BASE_SEED,
    sample: int     = None,
) -> None:
    t0 = time.time()

    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    rows = conn.execute(
        f"""SELECT DISTINCT date(date) AS d
            FROM titled_tuesday_standings
            WHERE date(date) >= '{start_date}'
            ORDER BY d"""
    ).fetchall()
    conn.close()

    dates = [r[0] for r in rows]
    if sample:
        dates = dates[:sample]
    n_total = len(dates)
    print(f'{n_total} tournament dates to backtest '
          f'({(dates[0] if dates else "?")} – {(dates[-1] if dates else "?")})\n')

    for i, t_date in enumerate(dates):
        print(f'[{i+1:>3}/{n_total}] {t_date}')

        df, p_participate, app_counts = load_and_prepare(as_of=t_date)
        if df.empty:
            print('  SKIP: no historical data before this date')
            continue

        players, p_play, hist_composites, hist_wts = build_player_pool_score(
            df, p_participate, app_counts, min_appearances=5,
        )

        if len(players) < max(n_values):
            print(f'  SKIP: pool={len(players)} < {max(n_values)}')
            continue

        plays_ct, topn_ct = run_simulation_score(
            players, p_play, hist_composites, hist_wts,
            n_sims=n_sims, n_values=n_values, seed=base_seed + i,
        )
        results = build_results(
            players, p_play, plays_ct, topn_ct,
            n_sims=n_sims, n_values=n_values,
        )

        out_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in n_values]
        df_out = results[out_cols].reset_index()
        df_out['tourn_date'] = t_date

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'DELETE FROM backtest_predictions WHERE tourn_date = ?', (t_date,)
        )
        df_out[['tourn_date', 'username'] + out_cols].to_sql(
            'backtest_predictions', conn, if_exists='append', index=False,
        )
        conn.commit()
        conn.close()

        print(f'  pool={len(players):,}  wrote {len(df_out):,} rows  '
              f'{time.time()-t0:>7.1f}s elapsed')

    print(f'\nDone. Total time: {time.time()-t0:.1f}s')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--start',  default=START_DATE,
                        help='Earliest tournament date YYYY-MM-DD (default: %(default)s)')
    parser.add_argument('--sims',   type=int, default=N_SIMS,
                        help='Monte Carlo simulations per event (default: %(default)s)')
    parser.add_argument('--sample', type=int, default=None,
                        help='Only run first N dates (quick test)')
    args = parser.parse_args()

    run_backtest(
        start_date=args.start,
        n_sims=args.sims,
        sample=args.sample,
    )
