"""
Top-level pipeline entry point for Titled Tuesday MC predictions.

Two public functions:

  run_raw()      — load data, build player pool, run baseline simulation.
                   Call once per session (or after DB updates).

  run_adjusted() — apply manual attendance adjustments and re-simulate.
                   Accepts an optional raw_run to reuse the pre-built pool;
                   otherwise rebuilds from scratch.

Both can persist results to the DB via save_to_db / save_official flags.
"""

import sqlite3
from typing import NamedTuple

import numpy as np
import pandas as pd

from .config import DB_PATH, N_SIMS
from .data import load_and_prepare, get_username_mappings
from .simulation import build_player_pool_score, run_simulation_score, build_results

PRED_N_VALUES = [1, 3, 5, 8, 10]


class RawRun(NamedTuple):
    results_raw:     pd.DataFrame
    players:         list
    p_play:          np.ndarray   # raw p_participate per player (pool-sized)
    hist_composites: list
    hist_wts:        list
    p_raw:           pd.Series    # raw p_participate for all players (full index)


class AdjustedRun(NamedTuple):
    results:         pd.DataFrame
    players:         list
    p_play:          np.ndarray   # adjusted p_participate per player
    hist_composites: list
    hist_wts:        list


def run_raw(
    tourn_date: str,
    save_to_db: bool = False,
    n_sims: int = N_SIMS,
) -> RawRun:
    """Build player pool and run the baseline (unadjusted) simulation.

    save_to_db=True writes results to latest_model_predictions_raw.
    """
    print(f'Predicting for tournament date: {tourn_date}')

    df, p_raw, app_counts = load_and_prepare()
    players, p_play_raw, hist_composites, hist_wts = build_player_pool_score(
        df, p_raw, app_counts, min_appearances=5
    )
    print(f'  Pool: {len(players):,} players | simulating {n_sims:,} tournaments (raw)...')

    plays_ct, topn_ct = run_simulation_score(
        players, p_play_raw, hist_composites, hist_wts,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )
    results_raw = build_results(
        players, p_play_raw, plays_ct, topn_ct,
        n_sims=n_sims, n_values=PRED_N_VALUES,
    )

    if save_to_db:
        _save_raw_to_db(results_raw, tourn_date)

    return RawRun(results_raw, players, p_play_raw, hist_composites, hist_wts, p_raw)


def run_adjusted(
    tourn_date: str,
    cut_players: list[str] | None = None,
    keep_players: list[str] | None = None,
    p_participate_overrides: dict[str, float] | None = None,
    p_nudges: dict[str, float] | None = None,
    raw_run: RawRun | None = None,
    save_official: bool = False,
    n_sims: int = N_SIMS,
) -> AdjustedRun:
    """Apply attendance adjustments and run the adjusted simulation.

    Attendance adjustment priority (all accept player_information.player_name strings):
      p_nudges                → shift p_participate in log-odds space (applied first)
      cut_players             → p_participate = 0.0  (overrides nudges)
      p_participate_overrides → p_participate = specified value (overrides cuts)
      keep_players            → p_participate = 1.0  (highest priority)

    Accepts an optional raw_run to reuse an already-built player pool.
    save_official=True writes adjusted results to latest_model_predictions.
    """
    cut_players             = cut_players or []
    keep_players            = keep_players or []
    p_participate_overrides = p_participate_overrides or {}
    p_nudges                = p_nudges or {}

    if raw_run is None:
        raw_run = run_raw(tourn_date, save_to_db=False, n_sims=n_sims)

    players, p_play_raw, hist_composites, hist_wts, p_raw = (
        raw_run.players, raw_run.p_play, raw_run.hist_composites, raw_run.hist_wts, raw_run.p_raw
    )

    # ── Resolve player names → usernames ─────────────────────────────────────
    _, PLAYER_TO_USERNAME = get_username_mappings()

    nudges: dict[str, float] = {}
    for name, delta in p_nudges.items():
        u = PLAYER_TO_USERNAME.get(name)
        if u is None:
            print(f'  Warning: p_nudges key {name!r} not found in player_information')
        else:
            nudges[u] = float(delta)

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

    # ── Apply adjustments ─────────────────────────────────────────────────────
    p_adjusted = p_raw.copy()
    altered: set[str] = set()

    for username, delta in nudges.items():
        if username in p_adjusted.index:
            pv = float(np.clip(p_adjusted[username], 1e-7, 1.0 - 1e-7))
            logit = np.log(pv / (1.0 - pv))
            p_adjusted[username] = float(1.0 / (1.0 + np.exp(-np.clip(logit + delta, -30, 30))))
            altered.add(username)

    for username, val in overrides.items():
        if username in p_adjusted.index:
            p_adjusted[username] = val
            altered.add(username)

    if nudges:
        print(f'  Applying {len(nudges)} nudge(s) (log-odds shift)...')
    if overrides:
        n_cuts = sum(1 for v in overrides.values() if v == 0.0)
        print(f'  Applying {len(overrides)} hard override(s) ({n_cuts} cut(s) to 0, {len(overrides) - n_cuts} value(s))...')

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
        _save_adjusted_to_db(results, tourn_date, altered)

    return AdjustedRun(results, players, p_play_adj, hist_composites, hist_wts)


def _save_raw_to_db(results_raw: pd.DataFrame, tourn_date: str) -> None:
    keep_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in PRED_N_VALUES]
    out = results_raw[keep_cols].reset_index()
    out['tourn_date'] = tourn_date

    conn = sqlite3.connect(DB_PATH)
    out.to_sql('latest_model_predictions_raw', conn, if_exists='replace', index=False)
    conn.close()
    print(f'Saved {len(out):,} rows -> latest_model_predictions_raw (unadjusted)')


def _save_adjusted_to_db(
    results: pd.DataFrame,
    tourn_date: str,
    altered: set[str],
) -> None:
    keep_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in PRED_N_VALUES]
    out = results[keep_cols].reset_index()
    out['tourn_date'] = tourn_date
    out['attendance_altered'] = out['username'].isin(altered).astype(int)

    conn = sqlite3.connect(DB_PATH)
    out.to_sql('latest_model_predictions', conn, if_exists='replace', index=False)
    conn.close()

    n_altered = int(out['attendance_altered'].sum())
    print(f'Saved {len(out):,} rows -> latest_model_predictions ({n_altered} attendance_altered)')


def next_tourn_date() -> str:
    """Return the ISO date of the next Titled Tuesday (one week after the latest in DB)."""
    conn = sqlite3.connect(DB_PATH)
    last = conn.execute(
        'SELECT MAX(date) FROM titled_tuesday_standings'
    ).fetchone()[0]
    conn.close()
    return (pd.Timestamp(last) + pd.Timedelta(weeks=1)).date().isoformat()
