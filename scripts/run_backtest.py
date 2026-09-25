#!/usr/bin/env python3
"""
Walk-forward joint backtest of MC ranking-signal variants.

For every Titled Tuesday from --start (default 2025-11-04, the first single-
session TT) to the latest tournament in the DB, restricts training data to
everything before that date and runs the MC pipeline once per selected model.

Supported models (--models):
  score                (score*10000 + tiebreak)   — production default
  perf                 performance_rating (aroc-based Elo estimate)
  score_smooth         score-composite with WIDE gaussian smoothing   (sigma = 0.5 score-points)
  score_smooth_narrow  score-composite with NARROW gaussian smoothing (sigma ~ 2x tiebreak scale)

Attendance is computed from the single-session era only (>= 2025-09-02),
regardless of how far back skill history is allowed to reach.

Predictions and actual outcomes are stored jointly in the `backtest` table.

Usage
-----
    python scripts/run_backtest.py
    python scripts/run_backtest.py --models score_smooth_narrow
    python scripts/run_backtest.py --start 2025-11-04 --sims 100000
    python scripts/run_backtest.py --sample 3         # quick smoke test
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from src.config import (
    DB_PATH, N_SIMS, DATA_START,
    SCORE_GAUSSIAN_KERNEL_SIGMA_WIDE, SCORE_GAUSSIAN_KERNEL_SIGMA_NARROW,
)
from src.data import load_and_prepare
from src.simulation import (
    build_player_pool_score, build_player_pool_perf,
    run_simulation_ranked, build_results,
)

BACKTEST_START     = '2025-11-04'
N_VALUES           = [1, 3, 5, 8, 10]
BASE_SEED          = 42
MIN_APP            = 5
ALL_MODELS         = ('score', 'perf', 'score_smooth', 'score_smooth_narrow')
DEFAULT_MODELS     = ALL_MODELS

# Per-model gaussian-smoothing sigma applied to sampled signals in run_simulation_ranked.
# Units are composite-signal units (score * SCORE_COMPOSITE_SCALE + tiebreak).
MODEL_NOISE_SIGMA = {
    'score':               0.0,
    'perf':                0.0,
    'score_smooth':        SCORE_GAUSSIAN_KERNEL_SIGMA_WIDE,
    'score_smooth_narrow': SCORE_GAUSSIAN_KERNEL_SIGMA_NARROW,
}


def _ensure_table(conn: sqlite3.Connection) -> None:
    cols = ',\n            '.join(
        [f'P_top{k}_given_play  REAL' for k in N_VALUES]
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS backtest (
            tourn_date         TEXT,
            model              TEXT,
            username           TEXT,
            p_participate      REAL,
            {cols},
            played             INTEGER,
            actual_rank        REAL,
            PRIMARY KEY (tourn_date, model, username)
        )
    """)
    conn.commit()


def _run_one_model(
    kind: str, df, p_participate, app_counts,
    n_sims: int, seed: int,
) -> pd.DataFrame:
    if kind in ('score', 'score_smooth', 'score_smooth_narrow'):
        pool = build_player_pool_score(df, p_participate, app_counts,
                                       min_appearances=MIN_APP)
    elif kind == 'perf':
        pool = build_player_pool_perf(df, p_participate, app_counts,
                                      min_appearances=MIN_APP)
    else:
        raise ValueError(f'unknown model kind: {kind}')

    players, p_play, hist_signals, hist_wts = pool
    if len(players) < max(N_VALUES):
        return pd.DataFrame()

    plays_ct, topn_ct = run_simulation_ranked(
        players, p_play, hist_signals, hist_wts,
        n_sims=n_sims, n_values=N_VALUES, seed=seed,
        noise_sigma=MODEL_NOISE_SIGMA[kind],
    )
    return build_results(
        players, p_play, plays_ct, topn_ct,
        n_sims=n_sims, n_values=N_VALUES,
    )


def _fetch_actuals(conn: sqlite3.Connection, tourn_date: str) -> pd.DataFrame:
    """Return {username -> rank} for players who played on tourn_date."""
    return pd.read_sql_query(
        """SELECT username, rank
           FROM titled_tuesday_standings
           WHERE date(date) = ?""",
        conn, params=(tourn_date,),
    ).set_index('username')


def _write(
    conn: sqlite3.Connection, tourn_date: str, model: str,
    results: pd.DataFrame, actuals: pd.DataFrame,
) -> int:
    out = results[['p_participate'] +
                  [f'P_top{k}_given_play' for k in N_VALUES]].copy()
    out['played']      = out.index.isin(actuals.index).astype(int)
    out['actual_rank'] = out.index.map(actuals['rank']).astype(float)
    out = out.reset_index().rename(columns={'index': 'username'})
    out.insert(0, 'model',      model)
    out.insert(0, 'tourn_date', tourn_date)

    conn.execute(
        'DELETE FROM backtest WHERE tourn_date = ? AND model = ?',
        (tourn_date, model),
    )
    out.to_sql('backtest', conn, if_exists='append', index=False)
    return len(out)


def run_backtest(
    start_date: str = BACKTEST_START,
    n_sims: int     = N_SIMS,
    base_seed: int  = BASE_SEED,
    sample: int     = None,
    attendance_start: str = DATA_START,
    models: tuple  = DEFAULT_MODELS,
) -> None:
    t0 = time.time()

    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    rows = conn.execute(
        """SELECT DISTINCT date(date) AS d
           FROM titled_tuesday_standings
           WHERE date(date) >= ?
           ORDER BY d""",
        (start_date,),
    ).fetchall()
    dates = [r[0] for r in rows]
    if sample:
        dates = dates[:sample]
    n_total = len(dates)
    print(f'{n_total} tournament dates ({dates[0]} - {dates[-1]}) '
          f'| attendance_start={attendance_start} | sims={n_sims:,} '
          f'| models={list(models)}\n')

    for i, t_date in enumerate(dates):
        print(f'[{i+1:>3}/{n_total}] {t_date}')

        df, p_participate, app_counts = load_and_prepare(
            as_of=t_date, attendance_start=attendance_start,
        )
        if df.empty:
            print('  SKIP: no historical data before this date')
            continue

        actuals = _fetch_actuals(conn, t_date)
        if actuals.empty:
            print('  SKIP: no actuals for this date')
            continue

        n_perf_avail = df['performance_rating'].notna().sum()
        n_perf_total = len(df)
        perf_coverage = n_perf_avail / n_perf_total if n_perf_total else 0
        if perf_coverage < 0.90:
            print(f'  WARN: perf-rating coverage {perf_coverage:.1%} '
                  f'({n_perf_avail:,}/{n_perf_total:,} hist rows)')

        for j, model in enumerate(models):
            results = _run_one_model(
                model, df, p_participate, app_counts,
                n_sims=n_sims, seed=base_seed + i * 10 + j,
            )
            if results.empty:
                print(f'  SKIP {model}: pool too small')
                continue
            n_written = _write(conn, t_date, model, results, actuals)
            n_played  = int(results.index.isin(actuals.index).sum())
            print(f'  {model:5s}: pool={len(results):,}  '
                  f'played={n_played:,}  rows_written={n_written:,}')

        conn.commit()
        print(f'  elapsed={time.time()-t0:>6.1f}s')

    conn.close()
    print(f'\nDone. Total time: {time.time()-t0:.1f}s')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--start',            default=BACKTEST_START)
    parser.add_argument('--attendance-start', default=DATA_START)
    parser.add_argument('--sims',             type=int, default=N_SIMS)
    parser.add_argument('--sample',           type=int, default=None)
    parser.add_argument('--models',           nargs='+', default=list(DEFAULT_MODELS),
                        choices=list(ALL_MODELS),
                        help='which model variants to run')
    args = parser.parse_args()

    run_backtest(
        start_date       = args.start,
        n_sims           = args.sims,
        sample           = args.sample,
        attendance_start = args.attendance_start,
        models           = tuple(args.models),
    )
