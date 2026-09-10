"""
Kalshi portfolio evaluation: build, simulate P&L, hedge analysis, Kelly sizing.

Typical usage
-------------
    from src.data import load_and_prepare, get_username_mappings
    from src.simulation import build_player_pool
    from src.portfolio import build_portfolio, run_portfolio_mc
    from kalshi_core import pnl_summary

    df, p_participate, app_counts = load_and_prepare()
    players, p_play, hist_pcts, hist_wts = build_player_pool(df, p_participate, app_counts)

    _, PLAYER_TO_USERNAME = get_username_mappings()
    portfolio = build_portfolio(df_kalshi, players, PLAYER_TO_USERNAME)
    pnl, p_win = run_portfolio_mc(players, p_play, hist_pcts, hist_wts, portfolio)
    pnl_summary(pnl)
"""

import time
import numpy as np
import pandas as pd

from kalshi_core import pnl_summary, portfolio_kelly, kalshi_order_price  # noqa: F401 — re-exported for callers
from .config import N_SIMS, CHUNK, SEED


# ── Portfolio parsing ─────────────────────────────────────────────────────────

def build_portfolio(df_tt, players, player_to_username):
    """
    Convert a Kalshi portfolio DataFrame into numpy arrays for vectorized MC.

    Required columns in df_tt
    -------------------------
    marketTitle  : player display name
    n            : top-N threshold (int)
    position     : 'Yes' or 'No'
    volume       : payout if position resolves correctly
    cost         : amount paid for the position

    Returns a dict of parallel arrays (one entry per portfolio row).
    """
    player_index = {u: i for i, u in enumerate(players)}
    player_idx_list, not_found = [], []

    for name in df_tt['marketTitle']:
        idx = -1
        u = player_to_username.get(name)
        if u is not None and u in player_index:
            idx = player_index[u]
        player_idx_list.append(idx)
        if idx < 0:
            not_found.append(name)

    if not_found:
        print(f'  Warning: {len(not_found)} positions not in player pool '
              f'(will always count as losses): {not_found}')

    return {
        'player_idx': np.array(player_idx_list, dtype=np.int32),
        'n_values':   df_tt['n'].to_numpy(dtype=np.int32),
        'is_yes':     (df_tt['position'] == 'Yes').to_numpy(dtype=bool),
        'volumes':    df_tt['volume'].to_numpy(dtype=np.float64),
        'costs':      df_tt['cost'].to_numpy(dtype=np.float64),
    }


# ── Portfolio Monte Carlo ─────────────────────────────────────────────────────

def run_portfolio_mc(players, p_play, hist_pcts, hist_wts,
                     portfolio, n_sims=N_SIMS, chunk=CHUNK, seed=SEED):
    """Run rank-percentile MC. Returns (pnl_per_sim, p_win) where p_win is per-position win probability."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = int(portfolio['n_values'].max())

    port_idx     = portfolio['player_idx']
    port_n       = portfolio['n_values']
    port_is_yes  = portfolio['is_yes']
    port_volumes = portfolio['volumes']
    port_costs   = portfolio['costs']
    known        = port_idx >= 0

    total_cost   = port_costs.sum()
    pnl_per_sim  = np.empty(n_sims, dtype=np.float64)
    outcome_sum  = np.zeros(len(port_idx), dtype=np.float64)

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n), dtype=np.float32)
        for i in range(n):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        present = rng.random((c, n)) < p_play
        scores[~present] = -np.inf

        k     = min(max_N, n - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]
        valid = scores[row, order] > -np.inf

        matches    = order[:, :, None] == port_idx[None, None, :]
        matches   &= valid[:, :, None]
        has_match  = matches.any(axis=1)
        first_rank = np.where(has_match, np.argmax(matches, axis=1) + 1, max_N + 1)

        in_top            = first_rank <= port_n[None, :]
        in_top[:, ~known] = False
        outcome           = np.where(port_is_yes[None, :], in_top, ~in_top).astype(np.float32)
        outcome[:, ~known] = 0.0

        outcome_sum                  += outcome.sum(axis=0)
        revenue                      = (outcome * port_volumes[None, :]).sum(axis=1)
        pnl_per_sim[start:start + c] = revenue - total_cost

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return pnl_per_sim, outcome_sum / n_sims


def run_portfolio_mc_score(players, p_play, hist_composites, hist_wts,
                           portfolio, n_sims=N_SIMS, chunk=CHUNK, seed=SEED):
    """Rank by (score, tiebreak) composite. Returns (pnl_per_sim, p_win) where p_win is per-position win probability."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = int(portfolio['n_values'].max())

    port_idx     = portfolio['player_idx']
    port_n       = portfolio['n_values']
    port_is_yes  = portfolio['is_yes']
    port_volumes = portfolio['volumes']
    port_costs   = portfolio['costs']
    known        = port_idx >= 0

    total_cost   = port_costs.sum()
    pnl_per_sim  = np.empty(n_sims, dtype=np.float64)
    outcome_sum  = np.zeros(len(port_idx), dtype=np.float64)

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n), dtype=np.float64)
        for i in range(n):
            idx = rng.choice(len(hist_composites[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_composites[i][idx]

        present = rng.random((c, n)) < p_play
        scores[~present] = -np.inf

        k     = min(max_N, n - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]
        valid = scores[row, order] > -np.inf

        matches    = order[:, :, None] == port_idx[None, None, :]
        matches   &= valid[:, :, None]
        has_match  = matches.any(axis=1)
        first_rank = np.where(has_match, np.argmax(matches, axis=1) + 1, max_N + 1)

        in_top            = first_rank <= port_n[None, :]
        in_top[:, ~known] = False
        outcome           = np.where(port_is_yes[None, :], in_top, ~in_top).astype(np.float32)
        outcome[:, ~known] = 0.0

        outcome_sum                  += outcome.sum(axis=0)
        revenue                      = (outcome * port_volumes[None, :]).sum(axis=1)
        pnl_per_sim[start:start + c] = revenue - total_cost

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return pnl_per_sim, outcome_sum / n_sims


# ── Hedge correlation analysis ────────────────────────────────────────────────

def compute_hedge_correlations(players, p_play, hist_pcts, hist_wts,
                               portfolio, n_values=None,
                               n_sims=N_SIMS, chunk=CHUNK, seed=SEED,
                               min_participation=0.05):
    """
    Single-pass simulation that computes:
      - per-tournament portfolio P&L
      - Pearson correlation between each (player, N) YES bet and portfolio P&L

    Negative yes_corr = bet pays when portfolio loses (hedge candidate).

    Returns (df_corr, pnl) where df_corr is sorted by yes_corr ascending.
    """
    if n_values is None:
        n_values = [1, 3, 8]

    rng    = np.random.default_rng(seed)
    n_play = len(players)
    n_vals = sorted(n_values)
    n_Nv   = len(n_vals)

    port_max_N  = int(portfolio['n_values'].max())
    hedge_max_N = n_vals[-1]
    max_N       = max(port_max_N, hedge_max_N)

    port_idx     = portfolio['player_idx']
    port_n       = portfolio['n_values']
    port_is_yes  = portfolio['is_yes']
    port_volumes = portfolio['volumes']
    port_costs   = portfolio['costs']
    known        = port_idx >= 0
    total_cost   = port_costs.sum()

    sum_X    = 0.0
    sum_X_sq = 0.0
    sum_Y    = np.zeros((n_Nv, n_play), dtype=np.float64)
    sum_XY   = np.zeros((n_Nv, n_play), dtype=np.float64)
    pnl_all  = np.empty(n_sims, dtype=np.float64)

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n_play), dtype=np.float32)
        for i in range(n_play):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        present = rng.random((c, n_play)) < p_play
        scores[~present] = -np.inf

        k     = min(max_N, n_play - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]
        valid = scores[row, order] > -np.inf

        matches     = order[:, :port_max_N, None] == port_idx[None, None, :]
        matches    &= valid[:, :port_max_N, None]
        has_match   = matches.any(axis=1)
        first_rank  = np.where(has_match, np.argmax(matches, axis=1) + 1, port_max_N + 1)
        in_top_port = first_rank <= port_n[None, :]
        in_top_port[:, ~known] = False
        outcome     = np.where(port_is_yes[None, :], in_top_port, ~in_top_port).astype(np.float32)
        outcome[:, ~known] = 0.0
        pnl_chunk   = (outcome * port_volumes[None, :]).sum(axis=1) - total_cost

        pnl_all[start:start + c] = pnl_chunk
        sum_X    += float(pnl_chunk.sum())
        sum_X_sq += float(np.dot(pnl_chunk, pnl_chunk))

        chunk_Y  = np.zeros(n_play, dtype=np.float64)
        chunk_XY = np.zeros(n_play, dtype=np.float64)
        n_val_ptr = 0
        pnl_f64 = pnl_chunk.astype(np.float64)
        for r in range(hedge_max_N):
            sim_rows     = np.where(valid[:, r])[0]
            players_here = order[sim_rows, r]
            np.add.at(chunk_Y,  players_here, 1.0)
            np.add.at(chunk_XY, players_here, pnl_f64[sim_rows])
            if n_val_ptr < n_Nv and (r + 1) == n_vals[n_val_ptr]:
                sum_Y[n_val_ptr]  += chunk_Y
                sum_XY[n_val_ptr] += chunk_XY
                n_val_ptr += 1

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    E_X   = sum_X / n_sims
    std_X = np.sqrt(max(sum_X_sq / n_sims - E_X ** 2, 0.0))
    E_Y   = sum_Y / n_sims
    E_XY  = sum_XY / n_sims
    cov   = E_XY - E_X * E_Y
    std_Y = np.sqrt(np.maximum(E_Y * (1.0 - E_Y), 1e-10))
    corr  = np.where(std_Y > 1e-6, cov / (std_X * std_Y), 0.0)

    part_mask = p_play >= min_participation
    dfs = []
    for ni, N in enumerate(n_vals):
        dfs.append(pd.DataFrame({
            'username': [players[i] for i in range(n_play) if part_mask[i]],
            'n':        N,
            'p_topN':   E_Y[ni, part_mask],
            'yes_corr': corr[ni, part_mask].astype(float),
            'no_corr':  (-corr[ni, part_mask]).astype(float),
        }))

    df_corr = (
        pd.concat(dfs, ignore_index=True)
        .sort_values('yes_corr')
        .reset_index(drop=True)
    )
    return df_corr, pnl_all


def show_best_hedges(df_corr, top_k=20, username_to_player=None):
    def name(u):
        return username_to_player.get(u, u) if username_to_player else u

    df = df_corr.copy()
    df['player'] = df['username'].apply(name)

    print('\n── Best YES hedges (negative correlation with portfolio) ────────')
    cols = ['player', 'n', 'p_topN', 'yes_corr']
    print(df.nsmallest(top_k, 'yes_corr')[cols]
          .rename(columns={'p_topN': 'P(top N)', 'yes_corr': 'corr(YES, portfolio)'})
          .to_string(index=False, float_format=lambda x: f'{x:.4f}'))

    print('\n── Best NO hedges (negative correlation with portfolio) ─────────')
    cols = ['player', 'n', 'p_topN', 'no_corr']
    print(df.nsmallest(top_k, 'no_corr')[cols]
          .rename(columns={'p_topN': 'P(top N)', 'no_corr': 'corr(NO, portfolio)'})
          .to_string(index=False, float_format=lambda x: f'{x:.4f}'))


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def run_payoff_matrix(players, p_play, hist_pcts, hist_wts,
                      portfolio, n_sims=N_SIMS, chunk=CHUNK, seed=SEED):
    """
    Simulate tournaments and return a (n_sims, n_positions) payoff matrix
    for Kelly optimisation.

    payoff[s, i] = (1 - c_i)  if position i resolves in your favour
                 = -c_i        otherwise
    where c_i = cost / volume (cost per share).
    """
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = int(portfolio['n_values'].max())
    P     = len(portfolio['player_idx'])

    port_idx    = portfolio['player_idx']
    port_n      = portfolio['n_values']
    port_is_yes = portfolio['is_yes']
    port_vols   = portfolio['volumes']
    port_costs  = portfolio['costs']
    known       = port_idx >= 0
    cps         = np.where(port_vols > 0, port_costs / port_vols, 0.0)

    payoff_matrix = np.empty((n_sims, P), dtype=np.float64)
    payoff_matrix[:, ~known] = -cps[~known]

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c   = min(chunk, n_sims - start)
        sl  = slice(start, start + c)
        row = np.arange(c)[:, None]

        scores = np.empty((c, n), dtype=np.float32)
        for i in range(n):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        present = rng.random((c, n)) < p_play
        scores[~present] = -np.inf

        k     = min(max_N, n - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        order = part[row, np.argsort(-scores[row, part], axis=1)]
        valid = scores[row, order] > -np.inf

        matches    = order[:, :, None] == port_idx[None, None, :]
        matches   &= valid[:, :, None]
        has_match  = matches.any(axis=1)
        first_rank = np.where(has_match, np.argmax(matches, axis=1) + 1, max_N + 1)

        in_top            = first_rank <= port_n[None, :]
        in_top[:, ~known] = False
        win = np.where(port_is_yes[None, :], in_top, ~in_top)
        win[:, ~known] = False

        payoff_matrix[sl] = np.where(win, 1.0 - cps[None, :], -cps[None, :])
        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return payoff_matrix, cps

