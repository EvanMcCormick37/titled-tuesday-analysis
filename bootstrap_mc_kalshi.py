#!/usr/bin/env python3
"""
Titled Tuesday — Kalshi Portfolio Monte Carlo

Vectorised extension of bootstrap_mc.py.  Instead of the slow pattern of
calling run_simulation(n_sims=1) inside a Python loop and evaluating the
portfolio after each single tournament, this module resolves every position
as a binary win/loss inside the chunk loop and accumulates per-tournament
P&L across all simulated tournaments in one pass.

Typical notebook usage
----------------------
    from bootstrap_mc_kalshi import (
        load_and_prepare, build_player_pool,
        build_portfolio, run_portfolio_mc, pnl_summary,
    )

    df, p_participate, app_counts = load_and_prepare(
        cut_players=['Magnus Carlsen'],
        player_to_username=PLAYER_TO_USERNAME,
    )
    players, p_play, hist_pcts, hist_wts = build_player_pool(
        df, p_participate, app_counts
    )
    portfolio = build_portfolio(df_tt, players, PLAYER_TO_USERNAME)
    pnl = run_portfolio_mc(players, p_play, hist_pcts, hist_wts, portfolio,
                           n_sims=100_000)
    pnl_summary(pnl)
"""

import time
import numpy as np
import pandas as pd
from datetime import datetime
import csv
import joblib
from sklearn.isotonic import IsotonicRegression

with open('data/username_to_player.csv', mode='r') as f:
    reader = csv.reader(f)
    USERNAME_TO_PLAYER = dict(reader)
    PLAYER_TO_USERNAME = {value: key for key, value in USERNAME_TO_PLAYER.items()}


# ── Hyperparameters ───────────────────────────────────────────────────────────
DATA_CUTOFF         = '2022-02-01'
SKILL_DECAY         = 0.975
PARTICIPATION_DECAY = 0.85
MIN_PARTICIPATION_RATE = 0.005
N_SIMS              = 100_000
N_VALUES            = [1, 3, 8]
MIN_P               = 0.0
MIN_APPEARANCES     = 0
CHUNK               = 10_000
SEED                = 42
CUT_PLAYERS = []
KEEP_PLAYERS = []

# ── Data preparation ──────────────────────────────────────────────────────────

def load_and_prepare(cut_players=CUT_PLAYERS, keep_players=KEEP_PLAYERS,
                     attendance_model='decay', target_date=None):
    """
    attendance_model : how to compute p_participate for each player.
        'decay'         – recency-weighted EWMA (original behaviour, default)
        'gradient_boost'– gradient boosted trees (attendance_models.py)
        'random_forest' – random forest (attendance_models.py)
        'logistic'      – logistic regression (attendance_models.py)

    target_date : pd.Timestamp or str used by the ML models to compute
        features; defaults to the next Tuesday after the last date in the data.
        Ignored when attendance_model='decay'.
    """
    cut_users  = [PLAYER_TO_USERNAME[player] for player in cut_players]
    keep_users = [PLAYER_TO_USERNAME[player] for player in keep_players]

    df = pd.read_csv('data/titled_tuesday_standings.csv', parse_dates=['date'])
    df = df[df['date'] >= '2022-02-08'].copy()

    # Keep best rank per player per event (handles rare duplicate entries)
    df = (
        df.sort_values('rank')
          .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
    )
    time_diff = (pd.Timestamp.now() - pd.to_datetime(df['date'])) // pd.Timedelta(weeks=1)
    N = (datetime.now() - datetime(2022, 2, 8)) // pd.Timedelta(weeks=1)
    df['skill_w'] = SKILL_DECAY ** time_diff
    df['part_w']  = PARTICIPATION_DECAY ** time_diff

    # Rank percentile: 1.0 = winner, 0.0 = last place
    n_in_event     = df.groupby('tournament_slug')['username'].transform('count')
    df['rank_pct'] = 1.0 - (df['rank'].astype(float) - 1) / (n_in_event - 1)

    # ── P(player enters next event) ────────────────────────────────────────────
    # Always compute the EWMA baseline; it serves as the fallback for players
    # who have too few appearances for the ML models (< 3 unique weeks).
    Z_part        = sum(PARTICIPATION_DECAY ** k for k in range(N))
    p_participate = (df.groupby('username')['part_w'].sum() / Z_part).clip(lower=MIN_PARTICIPATION_RATE).rename('p_participate')

    # Adding an Isotonic Regression model to act as a floor for p(participate)
    iso_reg = joblib.load('models/isotonic_regression.joblib')
    p_participate_np = p_participate.to_numpy()
    iso_floor = np.where((p_participate_np > .05), p_participate_np, iso_reg.predict(p_participate_np))
    p_participate = pd.Series(iso_floor, index=p_participate.index, name='p_participate')

    if attendance_model != 'decay':
        try:
            from attendance_models import (
                load_data as _am_load, get_weekly_slots,
                load_models as _am_load_models, get_p_participate_for_mc,
            )
            if target_date is None:
                last_date  = df['date'].max()
                target_date = last_date + pd.Timedelta(weeks=1)

            _df   = _am_load()
            _slots = get_weekly_slots(_df)
            _mdls  = _am_load_models()
            ml_p   = get_p_participate_for_mc(_df, _slots, _mdls,
                                              pd.Timestamp(target_date),
                                              model_name=attendance_model)
            n_updated = ml_p.index.isin(p_participate.index).sum()
            p_participate.update(ml_p)
            print(f'  p_participate: {attendance_model} model '
                  f'({n_updated:,} players, {len(p_participate) - n_updated:,} EWMA fallback)')
        except Exception as e:
            print(f'  p_participate: falling back to EWMA ({e})')
    else:
        print(f'  p_participate: decay-weighted EWMA with Isotonic Gaussian participation floor (@p < .05)')

    # Honour explicit cut / keep overrides
    p_participate[p_participate.index.isin(cut_users)]  = 0.0
    p_participate[p_participate.index.isin(keep_users)] = 1.0

    app_counts = df.groupby('username')['tournament_slug'].nunique().rename('appearances')
    print(f'  {N} events | {df["username"].nunique():,} unique players '
          f'| {df["date"].min().date()} to {df["date"].max().date()}')
    return df, p_participate, app_counts


def build_player_pool(df, p_participate, app_counts,
                      min_appearances=MIN_APPEARANCES, min_p=MIN_P):
    """Return arrays needed for simulation, filtered to eligible players."""
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
        g   = grp.get_group(u)
        pct = g['rank_pct'].to_numpy(dtype=np.float32)
        w   = g['skill_w'].to_numpy(dtype=np.float64, copy=True)
        w  /= w.sum()
        hist_pcts.append(pct)
        hist_wts.append(w)

    print(f'  Player pool: {len(players):,}')
    return players, p_play, hist_pcts, hist_wts


# ── Simulation ────────────────────────────────────────────────────────────────

def run_simulation(players, p_play, hist_pcts, hist_wts,
                   n_sims=N_SIMS, n_values=N_VALUES, chunk=CHUNK, seed=SEED):
    rng   = np.random.default_rng()
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        # Draw one rank_pct per player per sim from their weighted empirical distribution
        scores = np.empty((c, n), dtype=np.float32)
        for i in range(n):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        # Draw field presence; mask absent players out of the ranking
        present = rng.random((c, n)) < p_play
        plays_ct += present.sum(axis=0)
        scores[~present] = -np.inf

        # Find top-max_N players per sim via partial sort, then rank the winners
        part  = np.argpartition(-scores, min(max_N, n - 1), axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]  # shape (c, max_N)

        for k in n_values:
            top_k = order[:, :k]                          # shape (c, k)
            valid = scores[row, top_k] > -np.inf          # exclude if field < k players
            np.add.at(topn_ct[k], top_k[valid], 1)

        # print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return plays_ct, topn_ct


# ── Output ────────────────────────────────────────────────────────────────────

def build_results(players, p_play, plays_ct, topn_ct, n_sims, n_values=N_VALUES):
    rows = {'p_participate': p_play}
    for k in n_values:
        with np.errstate(invalid='ignore', divide='ignore'):
            cond = np.where(plays_ct > 0, topn_ct[k] / plays_ct, 0.0)
        rows[f'P_top{k}_given_play'] = cond
        rows[f'P_top{k}']           = topn_ct[k] / n_sims
        rows[f'ip_adv_top{k}'] = cond / (topn_ct[k] / n_sims)
    return pd.DataFrame(rows, index=pd.Index(players, name='username'))


def print_top(results, sort_col, n=20, n_values=N_VALUES):
    cols = ['p_participate'] + [
        c for k in n_values for c in (f'P_top{k}_given_play', f'P_top{k}')
    ]
    print(results.nlargest(n, sort_col)[cols].to_string(float_format=lambda x: f'{x:.4f}'))


# ── Portfolio parsing ─────────────────────────────────────────────────────────

def build_portfolio(df_tt, players, player_to_username):
    """
    Convert a Kalshi portfolio DataFrame into numpy arrays for vectorized
    Monte Carlo evaluation.

    df_tt required columns
    ----------------------
    marketTitle  : player display name (key into player_to_username)
    n            : top-N threshold (int)
    position     : 'Yes' or 'No'
    volume       : payout if position resolves correctly
    cost         : amount paid for the position

    Returns a dict of parallel arrays, one entry per portfolio row:
      player_idx  – index into `players`; -1 when player is not in the pool
      n_values    – top-N threshold
      is_yes      – True for YES positions
      volumes     – payout if correct
      costs       – cost paid
    """
    player_index = {u: i for i, u in enumerate(players)}

    player_idx_list = []
    not_found = []
    for i, name in enumerate(df_tt['marketTitle']):
        idx = -1
        u = player_to_username[name]
        if u in player_index:
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
    """
    Run Monte Carlo and return per-tournament P&L for a Kalshi portfolio.

    Every simulated tournament resolves each position as a binary win/loss
    (matching the behaviour of display_profits when run_simulation is called
    with n_sims=1).  Portfolio evaluation is fully vectorised inside the
    chunk loop, so this scales to hundreds of thousands of simulations
    without the overhead of a Python loop over individual tournaments.

    Parameters
    ----------
    players, p_play, hist_pcts, hist_wts : from build_player_pool
    portfolio : from build_portfolio
    n_sims    : total tournaments to simulate
    chunk     : sims per memory chunk
    seed      : RNG seed

    Returns
    -------
    pnl : np.ndarray, shape (n_sims,)
        Net profit / loss for each simulated tournament
        (positive = gain, negative = loss).
    """
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = int(portfolio['n_values'].max())

    port_idx     = portfolio['player_idx']   # (P,)
    port_n       = portfolio['n_values']     # (P,)
    port_is_yes  = portfolio['is_yes']       # (P,)
    port_volumes = portfolio['volumes']      # (P,)
    port_costs   = portfolio['costs']        # (P,)
    known        = port_idx >= 0             # (P,) — player found in pool

    total_cost  = port_costs.sum()
    pnl_per_sim = np.empty(n_sims, dtype=np.float64)

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        # ── Draw rank percentiles ──────────────────────────────────────
        scores = np.empty((c, n), dtype=np.float32)
        for i in range(n):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        # ── Field presence: absent players get -inf ────────────────────
        present = rng.random((c, n)) < p_play
        scores[~present] = -np.inf

        # ── Rank the top-max_N players via partial sort ────────────────
        k     = min(max_N, n - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]  # (c, max_N)
        valid = scores[row, order] > -np.inf                        # (c, max_N)

        # ── Vectorised rank lookup for every portfolio position ────────
        # matches[sim, rank_slot, portfolio_pos] = True when the player at
        # that rank slot is the player for that portfolio position.
        matches  = order[:, :, None] == port_idx[None, None, :]    # (c, max_N, P)
        matches &= valid[:, :, None]                                # mask absent

        has_match  = matches.any(axis=1)                            # (c, P)
        first_rank = np.where(
            has_match,
            np.argmax(matches, axis=1) + 1,   # 1-indexed rank
            max_N + 1,                         # sentinel: not in top-max_N
        )                                                           # (c, P)

        # ── Resolve win / loss for each position ───────────────────────
        in_top             = first_rank <= port_n[None, :]          # (c, P)
        in_top[:, ~known]  = False                                  # unknown → loss

        # outcome = 1 if position resolves in our favour, else 0
        outcome            = np.where(port_is_yes[None, :], in_top, ~in_top).astype(np.float32)
        outcome[:, ~known] = 0.0

        revenue                      = (outcome * port_volumes[None, :]).sum(axis=1)
        pnl_per_sim[start:start + c] = revenue - total_cost

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return pnl_per_sim


# ── Score/tiebreak simulation (alternative to rank-percentile) ────────────────
#
# Drop-in replacements: build_player_pool_score → run_simulation_score →
# build_results / print_top    (or run_portfolio_mc_score → pnl_summary)
#
# Instead of sampling a rank-percentile in [0,1], each player draws a
# (score, tiebreak) pair from their weighted history.  The two values are
# merged into a single sortable float:
#
#   composite = score * _SCORE_COMPOSITE_SCALE + tie_break
#
# Ranking is then performed on composites (descending), which correctly
# respects both the primary (score) and secondary (tiebreak) ordering.

_SCORE_COMPOSITE_SCALE = 10_000.0  # tiebreaks must stay below this


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
        composite = (g['score'].to_numpy(dtype=np.float64) * _SCORE_COMPOSITE_SCALE
                     + g['tie_break'].fillna(0).to_numpy(dtype=np.float64))
        w = g['skill_w'].to_numpy(dtype=np.float64, copy=True)
        w /= w.sum()
        hist_composites.append(composite)
        hist_wts.append(w)

    print(f'  Player pool: {len(players):,}')
    return players, p_play, hist_composites, hist_wts


def run_simulation_score(players, p_play, hist_composites, hist_wts,
                         n_sims=N_SIMS, n_values=N_VALUES, chunk=CHUNK, seed=SEED):
    """Like run_simulation but ranks by (score, tiebreak) instead of rank_pct."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    t0 = time.time()
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


def run_portfolio_mc_score(players, p_play, hist_composites, hist_wts,
                           portfolio, n_sims=N_SIMS, chunk=CHUNK, seed=SEED):
    """Like run_portfolio_mc but ranks by (score, tiebreak) instead of rank_pct."""
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = int(portfolio['n_values'].max())

    port_idx     = portfolio['player_idx']
    port_n       = portfolio['n_values']
    port_is_yes  = portfolio['is_yes']
    port_volumes = portfolio['volumes']
    port_costs   = portfolio['costs']
    known        = port_idx >= 0

    total_cost  = port_costs.sum()
    pnl_per_sim = np.empty(n_sims, dtype=np.float64)

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

        matches  = order[:, :, None] == port_idx[None, None, :]
        matches &= valid[:, :, None]

        has_match  = matches.any(axis=1)
        first_rank = np.where(
            has_match,
            np.argmax(matches, axis=1) + 1,
            max_N + 1,
        )

        in_top            = first_rank <= port_n[None, :]
        in_top[:, ~known] = False

        outcome            = np.where(port_is_yes[None, :], in_top, ~in_top).astype(np.float32)
        outcome[:, ~known] = 0.0

        revenue                      = (outcome * port_volumes[None, :]).sum(axis=1)
        pnl_per_sim[start:start + c] = revenue - total_cost

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return pnl_per_sim


# ── Hedge correlation analysis ───────────────────────────────────────────────

def compute_hedge_correlations(players, p_play, hist_pcts, hist_wts,
                               portfolio, n_values=[1, 3, 8],
                               n_sims=N_SIMS, chunk=CHUNK, seed=SEED,
                               min_participation=0.05):
    """
    In a single simulation pass, compute both the existing portfolio's
    per-tournament P&L and the binary top-N outcome for every player.
    Then return the Pearson correlation between each possible (player, N)
    bet and the portfolio P&L.

    A bet with strongly negative correlation pays out when the existing
    portfolio loses — these are the best hedge candidates.

    The correlation is computed from running accumulators (sum, sum-of-squares,
    and cross-product) so the full (n_sims × n_players) matrix is never
    materialised.  In-top membership for all players is tracked via scatter-add
    over ranked slots, which is O(chunk × max_N) memory.

    Parameters
    ----------
    min_participation : float
        Exclude players whose P(enter) is below this threshold from results.

    Returns
    -------
    df_corr : pd.DataFrame
        One row per (player, N) pair with columns:
          username, n, p_topN, yes_corr, no_corr
        yes_corr — correlation of a YES bet with portfolio P&L
        no_corr  — correlation of a NO  bet with portfolio P&L (= −yes_corr)
        Sorted by yes_corr ascending: best YES-hedge candidates first.
    pnl : np.ndarray, shape (n_sims,)
        Per-tournament portfolio P&L (identical to run_portfolio_mc output
        with the same seed).
    """
    rng    = np.random.default_rng(seed)
    n_play = len(players)
    n_vals = sorted(n_values)
    n_Nv   = len(n_vals)

    port_max_N   = int(portfolio['n_values'].max())
    hedge_max_N  = n_vals[-1]
    max_N        = max(port_max_N, hedge_max_N)

    port_idx     = portfolio['player_idx']
    port_n       = portfolio['n_values']
    port_is_yes  = portfolio['is_yes']
    port_volumes = portfolio['volumes']
    port_costs   = portfolio['costs']
    known        = port_idx >= 0
    total_cost   = port_costs.sum()

    # Running accumulators for Pearson correlation
    # X = portfolio P&L (scalar per sim)
    # Y[ni, player] = 1 if player is in top n_vals[ni] (binary per sim)
    sum_X    = 0.0
    sum_X_sq = 0.0
    sum_Y    = np.zeros((n_Nv, n_play), dtype=np.float64)   # E[Y] * n_sims
    sum_XY   = np.zeros((n_Nv, n_play), dtype=np.float64)   # E[XY] * n_sims

    pnl_all = np.empty(n_sims, dtype=np.float64)

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        # ── Draw rank percentiles ──────────────────────────────────────
        scores = np.empty((c, n_play), dtype=np.float32)
        for i in range(n_play):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        # ── Field presence ─────────────────────────────────────────────
        present = rng.random((c, n_play)) < p_play
        scores[~present] = -np.inf

        # ── Rank top-max_N players ─────────────────────────────────────
        k     = min(max_N, n_play - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        row   = np.arange(c)[:, None]
        order = part[row, np.argsort(-scores[row, part], axis=1)]  # (c, max_N)
        valid = scores[row, order] > -np.inf                        # (c, max_N)

        # ── Portfolio P&L (same logic as run_portfolio_mc) ─────────────
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

        # ── Accumulate in_top via scatter-add over rank slots ──────────
        # This avoids materialising a (chunk × n_players) array.
        # chunk_Y[player]  = # sims in this chunk where player is in top-r-so-far
        # chunk_XY[player] = sum of pnl for those sims
        chunk_Y  = np.zeros(n_play, dtype=np.float64)
        chunk_XY = np.zeros(n_play, dtype=np.float64)

        n_val_ptr = 0
        pnl_f64 = pnl_chunk.astype(np.float64)
        for r in range(hedge_max_N):
            sim_rows     = np.where(valid[:, r])[0]     # sims with a valid rank-r player
            players_here = order[sim_rows, r]
            np.add.at(chunk_Y,  players_here, 1.0)
            np.add.at(chunk_XY, players_here, pnl_f64[sim_rows])

            # Snapshot cumulative totals each time we hit a target N
            if n_val_ptr < n_Nv and (r + 1) == n_vals[n_val_ptr]:
                sum_Y[n_val_ptr]  += chunk_Y
                sum_XY[n_val_ptr] += chunk_XY
                n_val_ptr += 1

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    # ── Compute Pearson correlations ───────────────────────────────────────────
    E_X   = sum_X / n_sims
    std_X = np.sqrt(max(sum_X_sq / n_sims - E_X ** 2, 0.0))

    E_Y   = sum_Y / n_sims                                  # (n_Nv, n_play)
    E_XY  = sum_XY / n_sims                                 # (n_Nv, n_play)
    cov   = E_XY - E_X * E_Y                                # (n_Nv, n_play)

    # Var(Y) = E[Y](1 − E[Y])  since Y is binary
    std_Y = np.sqrt(np.maximum(E_Y * (1.0 - E_Y), 1e-10))  # (n_Nv, n_play)
    corr  = np.where(std_Y > 1e-6, cov / (std_X * std_Y), 0.0)

    # ── Build output DataFrame ─────────────────────────────────────────────────
    part_mask = p_play >= min_participation                  # exclude rarely-attending players
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
    """
    Print the top_k YES and top_k NO hedge candidates from compute_hedge_correlations.

    username_to_player : optional dict for display names; falls back to usernames.
    """
    def name(u):
        if username_to_player:
            return username_to_player.get(u, u)
        return u

    df = df_corr.copy()
    df['player'] = df['username'].apply(name)

    print(f'\n── Best YES hedges (negative correlation with portfolio) ────────')
    cols = ['player', 'n', 'p_topN', 'yes_corr']
    print(
        df.nsmallest(top_k, 'yes_corr')[cols]
        .rename(columns={'p_topN': 'P(top N)', 'yes_corr': 'corr(YES, portfolio)'})
        .to_string(index=False, float_format=lambda x: f'{x:.4f}')
    )

    print(f'\n── Best NO hedges (negative correlation with portfolio) ─────────')
    cols = ['player', 'n', 'p_topN', 'no_corr']
    print(
        df.nsmallest(top_k, 'no_corr')[cols]
        .rename(columns={'p_topN': 'P(top N)', 'no_corr': 'corr(NO, portfolio)'})
        .to_string(index=False, float_format=lambda x: f'{x:.4f}')
    )


# ── Summary statistics ────────────────────────────────────────────────────────

def pnl_summary(pnl, label='Portfolio'):
    """Print key risk / return statistics for a P&L distribution."""
    p = np.asarray(pnl)
    print(f'\n── {label} ({len(p):,} simulated tournaments) ──────────────')
    print(f'  Mean        : ${p.mean():+.2f}')
    print(f'  Std dev     : ${p.std():.2f}')
    print(f'  5th pctile  : ${np.percentile(p,  5):+.2f}')
    print(f'  25th pctile : ${np.percentile(p, 25):+.2f}')
    print(f'  Median      : ${np.median(p):+.2f}')
    print(f'  75th pctile : ${np.percentile(p, 75):+.2f}')
    print(f'  95th pctile : ${np.percentile(p, 95):+.2f}')
    print(f'  Worst case  : ${p.min():+.2f}')
    print(f'  Best case   : ${p.max():+.2f}')


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def run_payoff_matrix(players, p_play, hist_pcts, hist_wts,
                      portfolio, n_sims=N_SIMS, chunk=CHUNK, seed=SEED):
    """
    Simulate tournaments and return a (n_sims, n_positions) matrix of
    per-share net payoffs for portfolio Kelly optimisation.

    payoff[s, i] = (1 - c_i)  if position i resolves in your favour in sim s
                 = -c_i        otherwise
    where c_i = cost / volume for that portfolio position.

    Parameters
    ----------
    players, p_play, hist_pcts, hist_wts : from build_player_pool
    portfolio : from build_portfolio (volume should be 1 share per position
                so that cost == cost_per_share)

    Returns
    -------
    payoff_matrix : np.ndarray, shape (n_sims, n_positions)
    cost_per_share : np.ndarray, shape (n_positions,)
    """
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = int(portfolio['n_values'].max())
    P     = len(portfolio['player_idx'])

    port_idx    = portfolio['player_idx']   # (P,)
    port_n      = portfolio['n_values']     # (P,)
    port_is_yes = portfolio['is_yes']       # (P,)
    port_vols   = portfolio['volumes']      # (P,)
    port_costs  = portfolio['costs']        # (P,)
    known       = port_idx >= 0             # (P,)

    cps = np.where(port_vols > 0, port_costs / port_vols, 0.0)  # cost per share

    payoff_matrix = np.empty((n_sims, P), dtype=np.float64)
    payoff_matrix[:, ~known] = -cps[~known]  # unknown players always lose

    t0 = time.time()
    for start in range(0, n_sims, chunk):
        c  = min(chunk, n_sims - start)
        sl = slice(start, start + c)
        row = np.arange(c)[:, None]

        scores = np.empty((c, n), dtype=np.float32)
        for i in range(n):
            idx = rng.choice(len(hist_pcts[i]), size=c, p=hist_wts[i], replace=True)
            scores[:, i] = hist_pcts[i][idx]

        present = rng.random((c, n)) < p_play
        scores[~present] = -np.inf

        k     = min(max_N, n - 1)
        part  = np.argpartition(-scores, k, axis=1)[:, :max_N]
        order = part[row, np.argsort(-scores[row, part], axis=1)]  # (c, max_N)
        valid = scores[row, order] > -np.inf                        # (c, max_N)

        # Vectorised in-top check for all positions simultaneously
        matches    = order[:, :, None] == port_idx[None, None, :]  # (c, max_N, P)
        matches   &= valid[:, :, None]
        has_match  = matches.any(axis=1)                            # (c, P)
        first_rank = np.where(
            has_match, np.argmax(matches, axis=1) + 1, max_N + 1
        )                                                           # (c, P)

        in_top            = first_rank <= port_n[None, :]          # (c, P)
        in_top[:, ~known] = False

        win = np.where(port_is_yes[None, :], in_top, ~in_top)     # (c, P)
        win[:, ~known] = False

        payoff_matrix[sl] = np.where(win, 1.0 - cps[None, :], -cps[None, :])

        print(f'  {start + c:>7,} / {n_sims:,}  ({time.time() - t0:.1f}s)')

    return payoff_matrix, cps


def portfolio_kelly(payoff_matrix, cost_per_share, bankroll,
                    kelly_fraction=0.5, verbose=True):
    """
    Numerically optimise Kelly bet sizes for a portfolio of correlated bets.

    Maximises E[log(bankroll + PnL)] subject to:
      - shares_i >= 0  (long only)
      - sum_i(shares_i * cost_i) <= kelly_fraction * bankroll

    The kelly_fraction cap replaces the full-Kelly / fractional-Kelly
    distinction: set 0.5 for half-Kelly, 0.25 for quarter-Kelly, etc.

    Parameters
    ----------
    payoff_matrix   : (n_sims, P) array  — net payoff per share per sim
    cost_per_share  : (P,) array          — cost per share for each position
    bankroll        : float               — total capital available
    kelly_fraction  : float               — max fraction of bankroll to risk
    verbose         : bool

    Returns
    -------
    shares     : np.ndarray of int  — optimal integer share counts
    result     : scipy OptimizeResult
    """
    from scipy.optimize import minimize

    n_sims, P  = payoff_matrix.shape
    max_budget = kelly_fraction * bankroll

    # Estimate model EV per position from the payoff matrix to initialise
    ev_per_share = payoff_matrix.mean(axis=0)                          # (P,)
    init_kelly_f = (ev_per_share / (1.0 - cost_per_share)).clip(0)    # single-bet Kelly
    total_f = init_kelly_f.sum()
    if total_f > 1.0:
        init_kelly_f /= total_f
    x0 = (init_kelly_f * bankroll / cost_per_share.clip(1e-8)).clip(0)

    def neg_E_log(shares):
        pnl = bankroll + payoff_matrix @ shares
        return -np.mean(np.log(np.maximum(pnl, 1e-8)))

    def grad(shares):
        pnl = bankroll + payoff_matrix @ shares
        return -(payoff_matrix / np.maximum(pnl, 1e-8)[:, None]).mean(axis=0)

    constraints = [{
        'type': 'ineq',
        'fun':  lambda x: max_budget - cost_per_share @ x,
        'jac':  lambda x: -cost_per_share,
    }]
    bounds = [(0.0, None)] * P

    result = minimize(neg_E_log, x0, jac=grad, method='SLSQP',
                      bounds=bounds, constraints=constraints,
                      options={'ftol': 1e-10, 'maxiter': 1000})

    shares_opt = result.x
    total_cost = cost_per_share @ shares_opt
    if verbose:
        pnl = bankroll + payoff_matrix @ shares_opt
        print(f"Converged: {result.success}  |  "
              f"Exposure ${total_cost:.2f} ({total_cost/bankroll*100:.1f}% of bankroll)  |  "
              f"E[log-return]: {-result.fun - np.log(bankroll):.4f}")

    return np.round(shares_opt).astype(int), result

def kalshi_order_price(price, make = False):
    if make:
        return price + (0.0175 * price * (1.0 - price))
    else:
        return price + (0.07 * price * (1.0 - price))