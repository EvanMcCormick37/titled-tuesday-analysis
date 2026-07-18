"""
Core Monte Carlo simulation engine.

Two modes:
  rank-percentile  – build_player_pool       → run_simulation
  score/tiebreak   – build_player_pool_score → run_simulation_score

Both produce (plays_ct, topn_ct) arrays consumed by build_results / print_top.
"""

import time
import numpy as np
import pandas as pd

from .config import (
    N_SIMS, N_VALUES, CHUNK, SEED, MIN_P, MIN_APPEARANCES,
    _SCORE_COMPOSITE_SCALE,
)


# ── Player pool construction ──────────────────────────────────────────────────

def build_player_pool(df, p_participate, app_counts,
                      min_appearances=MIN_APPEARANCES, min_p=MIN_P):
    """Return arrays for rank-percentile simulation, filtered to eligible players."""
    eligible = p_participate[p_participate >= min_p].index
    if min_appearances > 0:
        eligible = eligible.intersection(
            app_counts[app_counts >= min_appearances].index
        )

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
    """Like build_player_pool but stores (score, tiebreak) composites instead of rank_pct."""
    eligible = p_participate[p_participate >= min_p].index
    if min_appearances > 0:
        eligible = eligible.intersection(
            app_counts[app_counts >= min_appearances].index
        )

    players = list(eligible)
    p_play  = p_participate.loc[players].to_numpy(dtype=np.float64)

    grp = df.groupby('username')
    hist_composites, hist_wts = [], []
    for u in players:
        g = grp.get_group(u)
        composite = (
            g['score'].to_numpy(dtype=np.float64) * _SCORE_COMPOSITE_SCALE
            + g['tie_break'].fillna(0).to_numpy(dtype=np.float64)
        )
        w = g['skill_w'].to_numpy(dtype=np.float64, copy=True)
        w /= w.sum()
        hist_composites.append(composite)
        hist_wts.append(w)

    print(f'  Player pool: {len(players):,}')
    return players, p_play, hist_composites, hist_wts


# ── Simulation kernels ────────────────────────────────────────────────────────

def run_simulation(players, p_play, hist_pcts, hist_wts,
                   n_sims=N_SIMS, n_values=N_VALUES, chunk=CHUNK, seed=SEED):
    """Rank-percentile MC. Returns (plays_ct, topn_ct)."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    t0 = time.time()
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


def run_simulation_score(players, p_play, hist_composites, hist_wts,
                         n_sims=N_SIMS, n_values=N_VALUES, chunk=CHUNK, seed=SEED):
    """Score/tiebreak MC. Returns (plays_ct, topn_ct)."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n), dtype=np.float64)
        for i in range(n):
            idx = rng.choice(len(hist_composites[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_composites[i][idx]

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
