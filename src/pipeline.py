"""
Top-level pipeline entry point for Titled Tuesday MC predictions.

run_predictions() is the single function called by the command-center notebook
and make_predictions.py.  It chains:

  load_and_prepare() (raw, no adjustments)
  → build_player_pool_score
  → run_simulation_score          (raw)
  → apply cut_players / keep_players / p_participate_overrides in memory
  → run_simulation_score          (adjusted)
  → optionally save to DB         (save_official=True)

Both simulations share the same player pool built once from historical data.
Only p_play differs between the two runs.
"""

import sqlite3
from typing import NamedTuple

import numpy as np
import pandas as pd

from .config import DB_PATH, N_SIMS
from .data import load_and_prepare, get_username_mappings
from .simulation import build_player_pool_score, run_simulation_score, build_results

PRED_N_VALUES = [1, 3, 5, 8, 10]


class PredictionRun(NamedTuple):
    """Output of run_predictions(). Carries both result sets and simulation arrays."""
    results:         pd.DataFrame   # adjusted predictions
    results_raw:     pd.DataFrame   # unadjusted predictions (pure model estimates)
    players:         list
    p_play:          np.ndarray     # adjusted p_participate per player
    hist_composites: list
    hist_wts:        list


def run_predictions(
    tourn_date: str,
    cut_players: list[str] | None = None,
    keep_players: list[str] | None = None,
    p_participate_overrides: dict[str, float] | None = None,
    save_official: bool = False,
    n_sims: int = N_SIMS,
) -> PredictionRun:
    """Run the full MC pipeline for tourn_date.

    Attendance adjustments are applied via three in-memory parameters (all take
    player_information.player_name strings, not chess.com usernames):

      cut_players              → p_participate = 0.0
      p_participate_overrides  → p_participate = specified value (overrides cuts)
      keep_players             → p_participate = 1.0 (highest priority)

    save_official=True writes results to the DB and records which players had
    their attendance manually adjusted (attendance_altered column).
    """
    cut_players             = cut_players or []
    keep_players            = keep_players or []
    p_participate_overrides = p_participate_overrides or {}

    print(f'Predicting for tournament date: {tourn_date}')

    # ── Load base data (no adjustments) ──────────────────────────────────────
    df, p_raw, app_counts = load_and_prepare()
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

    # ── Resolve player names → usernames, build override map ─────────────────
    _, PLAYER_TO_USERNAME = get_username_mappings()
    overrides: dict[str, float] = {}

    for name in cut_players:
        u = PLAYER_TO_USERNAME.get(name)
        if u is None:
            print(f'  Warning: cut_players entry {name!r} not found in player_information')
        else:
            overrides[u] = 0.0

    for name, val in p_participate_overrides.items():
        u = PLAYER_TO_USERNAME.get(name)
        if u is None:
            print(f'  Warning: p_participate_overrides key {name!r} not found in player_information')
        else:
            overrides[u] = float(val)

    for name in keep_players:
        u = PLAYER_TO_USERNAME.get(name)
        if u is None:
            print(f'  Warning: keep_players entry {name!r} not found in player_information')
        else:
            overrides[u] = 1.0

    # ── Apply overrides ───────────────────────────────────────────────────────
    p_adjusted = p_raw.copy()
    altered: set[str] = set()
    for username, val in overrides.items():
        if username in p_adjusted.index:
            p_adjusted[username] = val
            altered.add(username)

    if altered:
        n_cuts = sum(1 for u in altered if p_adjusted[u] == 0.0)
        print(f'  Applying {len(altered)} override(s) ({n_cuts} cut(s) to 0, {len(altered) - n_cuts} value(s))...')

    # ── Adjusted simulation ───────────────────────────────────────────────────
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

    if save_official:
        _save_to_db(results, results_raw, tourn_date, altered)

    return PredictionRun(results, results_raw, players, p_play_adj, hist_composites, hist_wts)


def _save_to_db(
    results: pd.DataFrame,
    results_raw: pd.DataFrame,
    tourn_date: str,
    altered: set[str],
) -> None:
    keep_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in PRED_N_VALUES]

    out = results[keep_cols].reset_index()
    out['tourn_date'] = tourn_date
    out['attendance_altered'] = out['username'].isin(altered).astype(int)

    out_raw = results_raw[keep_cols].reset_index()
    out_raw['tourn_date'] = tourn_date

    conn = sqlite3.connect(DB_PATH)
    out.to_sql('latest_model_predictions', conn, if_exists='replace', index=False)
    out_raw.to_sql('latest_model_predictions_raw', conn, if_exists='replace', index=False)
    conn.close()

    n_altered = int(out['attendance_altered'].sum())
    print(f'Saved {len(out):,} rows -> latest_model_predictions ({n_altered} attendance_altered)')
    print(f'Saved {len(out_raw):,} rows -> latest_model_predictions_raw (unadjusted)')


def drop_attendance_adjustments_table() -> None:
    """One-time migration: drop the attendance_adjustments table.

    This table is no longer used. Adjustments are now passed directly to
    run_predictions() as cut_players / keep_players / p_participate_overrides.
    Call this once after verifying you no longer need the historical rows.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DROP TABLE IF EXISTS attendance_adjustments')
    conn.commit()
    conn.close()
    print('Dropped attendance_adjustments table.')


def next_tourn_date() -> str:
    """Return the ISO date of the next Titled Tuesday (one week after the latest in DB)."""
    conn = sqlite3.connect(DB_PATH)
    last = conn.execute(
        'SELECT MAX(date) FROM titled_tuesday_standings'
    ).fetchone()[0]
    conn.close()
    return (pd.Timestamp(last) + pd.Timedelta(weeks=1)).date().isoformat()
