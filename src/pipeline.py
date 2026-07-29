"""
Top-level pipeline entry point for Titled Tuesday MC predictions.

run_predictions(tourn_date) is the single function called by make_predictions.py
and the command-center notebook.  It chains:
  load_and_prepare → build_player_pool_score → run_simulation_score → build_results

Returns a PredictionRun namedtuple that carries both the results DataFrame and
the raw simulation arrays needed for portfolio P&L analysis.
"""

import sqlite3
from typing import NamedTuple

import numpy as np
import pandas as pd

from .config import DB_PATH, N_SIMS, N_VALUES
from .data import load_and_prepare
from .simulation import build_player_pool_score, run_simulation_score, build_results

PRED_N_VALUES = [1, 3, 5, 8, 10]


class PredictionRun(NamedTuple):
    """Output of run_predictions().  Carries results and simulation arrays."""
    results:         pd.DataFrame   # indexed by username; P_top{N}, P_top{N}_given_play, etc.
    players:         list           # player list in simulation order
    p_play:          np.ndarray     # p_participate per player (same order as players)
    hist_composites: list           # per-player historical (score, tiebreak) composite arrays
    hist_wts:        list           # per-player skill-decay weights


def run_predictions(tourn_date: str, n_sims: int = N_SIMS) -> PredictionRun:
    """Run the full MC pipeline for tourn_date, applying active DB adjustments.

    Reads attendance_adjustments from the DB for tourn_date (nudges then
    overrides), builds the player pool, simulates n_sims tournaments, and
    returns a PredictionRun with the results DataFrame plus the raw simulation
    arrays needed for portfolio analysis.
    """
    print(f'Predicting for tournament date: {tourn_date}')

    df, p_participate, app_counts = load_and_prepare(tourn_date=tourn_date)
    players, p_play, hist_composites, hist_wts = build_player_pool_score(
        df, p_participate, app_counts, min_appearances=5
    )
    print(f'  Pool: {len(players):,} players | simulating {n_sims:,} tournaments...')
    plays_ct, topn_ct = run_simulation_score(
        players, p_play, hist_composites, hist_wts,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )
    results = build_results(
        players, p_play, plays_ct, topn_ct,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )
    return PredictionRun(results, players, p_play, hist_composites, hist_wts)


def save_predictions(run: PredictionRun, tourn_date: str) -> None:
    """Write prediction results to latest_model_predictions, replacing previous rows."""
    keep_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in PRED_N_VALUES]
    out = run.results[keep_cols].reset_index()
    out['tourn_date'] = tourn_date
    conn = sqlite3.connect(DB_PATH)
    out.to_sql('latest_model_predictions', conn, if_exists='replace', index=False)
    conn.close()
    print(f'Saved {len(out):,} rows -> latest_model_predictions (tourn_date={tourn_date})')


def next_tourn_date() -> str:
    """Return the ISO date of the next Titled Tuesday (one week after the latest in DB)."""
    conn = sqlite3.connect(DB_PATH)
    last = conn.execute(
        'SELECT MAX(date) FROM titled_tuesday_standings'
    ).fetchone()[0]
    conn.close()
    return (pd.Timestamp(last) + pd.Timedelta(weeks=1)).date().isoformat()
