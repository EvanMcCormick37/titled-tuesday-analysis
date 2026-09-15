"""
Core Monte Carlo simulation engine.

Three player-pool builders — one per skill signal:
  build_player_pool         rank-percentile (legacy)
  build_player_pool_score   score*10000 + tiebreak (production default)
  build_player_pool_perf    performance_rating (aroc-based Elo estimate)

All three feed into a single kernel:
  run_simulation_ranked (also exposed as run_simulation_score for back-compat)

The rank-percentile mode has its own kernel `run_simulation` for legacy callers.
"""

import time
import numpy as np
import pandas as pd

from .config import (
    N_SIMS, N_VALUES, CHUNK, SEED, MIN_P, MIN_APPEARANCES,
    _SCORE_COMPOSITE_SCALE,
)


# ── Player pool construction ──────────────────────────────────────────────────

def _eligible_players(p_participate, app_counts, min_appearances, min_p):
    idx = p_participate[p_participate >= min_p].index
    if min_appearances > 0:
        idx = idx.intersection(app_counts[app_counts >= min_appearances].index)
    return idx


def build_player_pool(df, p_participate, app_counts,
                      min_appearances=MIN_APPEARANCES, min_p=MIN_P):
    """Return arrays for rank-percentile simulation (legacy)."""
    eligible = _eligible_players(p_participate, app_counts, min_appearances, min_p)
    players = list(eligible)
    p_play  = p_participate.loc[players].to_numpy(dtype=np.float64)

    grp = df.groupby('username')
    hist_pcts, hist_wts = [], []
    for u in players:
        g  = grp.get_group(u)
        pct = g['rank_pct'].to_numpy(dtype=np.float32)
        w   = g['skill_w'].to_numpy(dtype=np.float64, copy=True)
        w  /= w.sum()
        hist_pcts.append(pct)
        hist_wts.append(w)

    print(f'  Player pool: {len(players):,}')
    return players, p_play, hist_pcts, hist_wts


def build_player_pool_score(df, p_participate, app_counts,
                            min_appearances=MIN_APPEARANCES, min_p=MIN_P):
    """Composite (score*scale + tiebreak) player pool — production default."""
    eligible = _eligible_players(p_participate, app_counts, min_appearances, min_p)
    players = list(eligible)
    p_play  = p_participate.loc[players].to_numpy(dtype=np.float64)

    grp = df.groupby('username')
    hist_signals, hist_wts = [], []
    for u in players:
        g = grp.get_group(u)
        signal = (
            g['score'].to_numpy(dtype=np.float64) * _SCORE_COMPOSITE_SCALE
            + g['tie_break'].fillna(0).to_numpy(dtype=np.float64)
        )
        w = g['skill_w'].to_numpy(dtype=np.float64, copy=True)
        w /= w.sum()
        hist_signals.append(signal)
        hist_wts.append(w)

    print(f'  Player pool (score): {len(players):,}')
    return players, p_play, hist_signals, hist_wts


def build_player_pool_perf(df, p_participate, app_counts,
                           min_appearances=MIN_APPEARANCES, min_p=MIN_P):
    """Performance-rating player pool. Drops rows with NaN performance_rating.

    A player is only eligible if they satisfy min_appearances *among rows with
    performance_rating present*. Weights are renormalised over the retained
    subset, so a player missing perf on some historical events is still usable
    (their remaining history is upweighted).
    """
    df_valid = df[df['performance_rating'].notna()]
    perf_app_counts = df_valid.groupby('username')['tournament_slug'].nunique()

    eligible = _eligible_players(p_participate, perf_app_counts,
                                 min_appearances, min_p)
    players = list(eligible)
    p_play  = p_participate.loc[players].to_numpy(dtype=np.float64)

    grp = df_valid.groupby('username')
    hist_signals, hist_wts = [], []
    for u in players:
        g = grp.get_group(u)
        signal = g['performance_rating'].to_numpy(dtype=np.float64)
        w = g['skill_w'].to_numpy(dtype=np.float64, copy=True)
        w /= w.sum()
        hist_signals.append(signal)
        hist_wts.append(w)

    print(f'  Player pool (perf-rating): {len(players):,}')
    return players, p_play, hist_signals, hist_wts


# ── Simulation kernels ────────────────────────────────────────────────────────

def run_simulation(players, p_play, hist_pcts, hist_wts,
                   n_sims=N_SIMS, n_values=N_VALUES, chunk=CHUNK, seed=SEED):
    """Rank-percentile MC (legacy, float32 signals). Returns (plays_ct, topn_ct)."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n), dtype=np.float32)
        for i in range(n):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        present = rng.random((c, n)) < p_play
        plays_ct += present.sum(axis=0)
        scores[~present] = -np.inf

        part  = np.argpartition(-scores, min(max_N, n - 1), axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]

        for k in n_values:
            top_k = order[:, :k]
            valid = scores[row, top_k] > -np.inf
            np.add.at(topn_ct[k], top_k[valid], 1)

    return plays_ct, topn_ct


def run_simulation_ranked(players, p_play, hist_signals, hist_wts,
                          n_sims=N_SIMS, n_values=N_VALUES, chunk=CHUNK, seed=SEED):
    """Generic ranked-signal MC (float64). Works for score-composite or perf-rating pools."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n), dtype=np.float64)
        for i in range(n):
            idx = rng.choice(len(hist_signals[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_signals[i][idx]

        present = rng.random((c, n)) < p_play
        plays_ct += present.sum(axis=0)
        scores[~present] = -np.inf

        part  = np.argpartition(-scores, min(max_N, n - 1), axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]

        for k in n_values:
            top_k = order[:, :k]
            valid = scores[row, top_k] > -np.inf
            np.add.at(topn_ct[k], top_k[valid], 1)

    return plays_ct, topn_ct


# Back-compat alias — existing callers (pipeline.py, notebooks) pass the same args.
run_simulation_score = run_simulation_ranked


# ── Output helpers ────────────────────────────────────────────────────────────

def build_results(players, p_play, plays_ct, topn_ct, n_sims, n_values=N_VALUES):
    rows = {'p_participate': p_play}
    for k in n_values:
        with np.errstate(invalid='ignore', divide='ignore'):
            cond = np.where(plays_ct > 0, topn_ct[k] / plays_ct, 0.0)
        rows[f'P_top{k}_given_play'] = cond
        rows[f'P_top{k}']           = topn_ct[k] / n_sims
        rows[f'ip_adv_top{k}']      = cond / (topn_ct[k] / n_sims)
    return pd.DataFrame(rows, index=pd.Index(players, name='username'))


def print_top(results, sort_col, n=20, n_values=N_VALUES):
    cols = ['p_participate'] + [
        c for k in n_values for c in (f'P_top{k}_given_play', f'P_top{k}')
    ]
    print(results.nlargest(n, sort_col)[cols].to_string(float_format=lambda x: f'{x:.4f}'))
