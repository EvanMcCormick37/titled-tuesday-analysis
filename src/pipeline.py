"""
Top-level pipeline entry point for Titled Tuesday MC predictions.

run_predictions(tourn_date) is the single function called by make_predictions.py
and the command-center notebook.  It chains:
  load_and_prepare (no adjustments) → build_player_pool_score → run_simulation_score (raw)
  apply_db_adjustments              → run_simulation_score (adjusted)

Both runs share the same player pool (hist_composites, hist_wts) built once from
historical data.  Only p_play differs between the two runs.

Returns a PredictionRun namedtuple with both result sets and the raw simulation
arrays needed for portfolio P&L analysis.
"""

import sqlite3
from typing import NamedTuple

import numpy as np
import pandas as pd

from .config import DB_PATH, N_SIMS, N_VALUES
from .data import load_and_prepare, apply_db_adjustments
from .simulation import build_player_pool_score, run_simulation_score, build_results

PRED_N_VALUES = [1, 3, 5, 8, 10]


class PredictionRun(NamedTuple):
    """Output of run_predictions().  Carries both result sets and simulation arrays."""
    results:         pd.DataFrame   # adjusted predictions (P_top{N}, P_top{N}_given_play, etc.)
    results_raw:     pd.DataFrame   # unadjusted predictions (no schedule overrides/nudges)
    players:         list           # player list in simulation order
    p_play:          np.ndarray     # adjusted p_participate per player
    hist_composites: list           # per-player historical (score, tiebreak) composite arrays
    hist_wts:        list           # per-player skill-decay weights


def run_predictions(tourn_date: str, n_sims: int = N_SIMS) -> PredictionRun:
    """Run the full MC pipeline for tourn_date, producing adjusted and raw predictions.

    Two simulations are run back-to-back sharing the same player pool:
      1. Raw — no attendance adjustments; reflects pure skill-based attendance rates.
         Saved to latest_model_predictions_raw.
      2. Adjusted — active nudges and overrides from attendance_adjustments applied.
         Saved to latest_model_predictions.

    The raw results are used to sort cut/override players by baseline P_top1 strength
    in the adjustment editor, where the adjusted values would all be zero for cut players.
    """
    print(f'Predicting for tournament date: {tourn_date}')

    # ── Load base data WITHOUT adjustments ────────────────────────────────────
    df, p_raw, app_counts = load_and_prepare(tourn_date=tourn_date, apply_adjustments=False)
    players, p_play_raw, hist_composites, hist_wts = build_player_pool_score(
        df, p_raw, app_counts, min_appearances=5
    )
    print(f'  Pool: {len(players):,} players | simulating {n_sims:,} tournaments (raw)...')

    # ── Raw simulation ────────────────────────────────────────────────────────
    plays_ct_raw, topn_ct_raw = run_simulation_score(
        players, p_play_raw, hist_composites, hist_wts,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )
    results_raw = build_results(
        players, p_play_raw, plays_ct_raw, topn_ct_raw,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )

    # ── Apply adjustments, re-simulate ───────────────────────────────────────
    p_adjusted = apply_db_adjustments(p_raw, tourn_date)
    p_play_adj = p_adjusted.loc[players].to_numpy(dtype=np.float64)

    print(f'  Simulating {n_sims:,} tournaments (adjusted)...')
    plays_ct_adj, topn_ct_adj = run_simulation_score(
        players, p_play_adj, hist_composites, hist_wts,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )
    results = build_results(
        players, p_play_adj, plays_ct_adj, topn_ct_adj,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )

    return PredictionRun(results, results_raw, players, p_play_adj, hist_composites, hist_wts)


def save_predictions(run: PredictionRun, tourn_date: str) -> None:
    """Write both prediction runs to the DB.

    latest_model_predictions     — adjusted (with schedule overrides/nudges)
    latest_model_predictions_raw — unadjusted (pure skill-based attendance rates)
    """
    keep_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in PRED_N_VALUES]

    out = run.results[keep_cols].reset_index()
    out['tourn_date'] = tourn_date

    out_raw = run.results_raw[keep_cols].reset_index()
    out_raw['tourn_date'] = tourn_date

    conn = sqlite3.connect(DB_PATH)
    out.to_sql('latest_model_predictions', conn, if_exists='replace', index=False)
    out_raw.to_sql('latest_model_predictions_raw', conn, if_exists='replace', index=False)
    conn.close()

    print(f'Saved {len(out):,} rows -> latest_model_predictions (adjusted)')
    print(f'Saved {len(out_raw):,} rows -> latest_model_predictions_raw (unadjusted)')


def next_tourn_date() -> str:
    """Return the ISO date of the next Titled Tuesday (one week after the latest in DB)."""
    conn = sqlite3.connect(DB_PATH)
    last = conn.execute(
        'SELECT MAX(date) FROM titled_tuesday_standings'
    ).fetchone()[0]
    conn.close()
    return (pd.Timestamp(last) + pd.Timedelta(weeks=1)).date().isoformat()
