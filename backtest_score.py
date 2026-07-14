#!/usr/bin/env python3
"""
Walk-forward backtest: score/tiebreak Monte Carlo model.

For each Titled Tuesday from START_DATE onward, trains on *all prior data*
(back to the CSV's earliest entry, ~2014) and records predicted top-N
probabilities for every participant in that tournament.

Output CSV columns
------------------
tournament_slug, date, username, rank, field_size, p_participate,
p_top1, p_top3, p_top8, p_top10,
p_top1_given_play, p_top3_given_play, p_top8_given_play, p_top10_given_play,
in_top1, in_top3, in_top8, in_top10

Usage
-----
    python backtest_score.py              # full run, saves to OUTPUT_PATH
    python backtest_score.py --sample 20  # quick sanity-check on first 20 events
"""

import argparse
import time
import numpy as np
import pandas as pd

from bootstrap_mc_kalshi import SKILL_DECAY, PARTICIPATION_DECAY, _SCORE_COMPOSITE_SCALE

# ── Defaults ──────────────────────────────────────────────────────────────────
START_DATE  = '2022-02-08'
N_VALUES    = [1, 3, 8, 10]
N_SIMS      = 10_000       # per tournament; SE on p_top1 ≈ 0.2 %
CHUNK       = 5_000
BASE_SEED   = 42
OUTPUT_PATH = 'data/backtest_score_predictions.csv'

# Converged geometric-series normaliser for p_participate.
# With PARTICIPATION_DECAY = 0.85 and 400+ weeks of data, the exact sum
# differs from this limit by < 0.001 %.
_Z_PART = 1.0 / (1.0 - PARTICIPATION_DECAY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _precompute_user_histories(df: pd.DataFrame) -> dict:
    """
    Group the full DataFrame by username once.
    Returns {username: {'dates': ndarray[datetime64], 'composites': ndarray[f64]}}.
    """
    df = df.copy()
    df['composite'] = (
        df['score'].fillna(0).to_numpy(dtype=np.float64) * _SCORE_COMPOSITE_SCALE
        + df['tie_break'].fillna(0).to_numpy(dtype=np.float64)
    )
    user_data = {}
    for username, grp in df.groupby('username'):
        user_data[username] = {
            'dates':      grp['date'].to_numpy(),               # datetime64[ns]
            'composites': grp['composite'].to_numpy(dtype=np.float64),
        }
    return user_data


def _build_pool_at(user_data: dict, cutoff: pd.Timestamp):
    """
    Build player-pool arrays using only entries with date < cutoff.

    Returns
    -------
    players         : list[str]
    p_play          : ndarray[f64]  – P(participates in next event)
    hist_composites : list[ndarray] – per-player composite arrays
    hist_wts        : list[ndarray] – normalised skill-decay weights
    """
    cutoff_np = np.datetime64(cutoff)

    players, p_play_list, hist_composites, hist_wts = [], [], [], []

    for username, data in user_data.items():
        mask = data['dates'] < cutoff_np
        if not mask.any():
            continue

        days_back  = (
            (cutoff - pd.DatetimeIndex(data['dates'][mask]))
            .days.to_numpy(dtype=np.float64)
        )
        weeks_back = (days_back / 7.0).astype(int)   # truncate, matches original

        skill_w = SKILL_DECAY ** weeks_back
        part_w  = PARTICIPATION_DECAY ** weeks_back

        p_part       = float(part_w.sum()) / _Z_PART
        skill_w_norm = skill_w / skill_w.sum()

        players.append(username)
        p_play_list.append(min(p_part, 1.0))          # cap: random() ∈ [0,1)
        hist_composites.append(data['composites'][mask])
        hist_wts.append(skill_w_norm)

    p_play = np.array(p_play_list, dtype=np.float64)
    return players, p_play, hist_composites, hist_wts


def _simulate(players, p_play, hist_composites, hist_wts,
              n_sims: int, n_values: list, chunk: int, seed: int):
    """
    Bootstrap simulation over score/tiebreak composites.

    Returns
    -------
    p_sim_part        : ndarray – simulated P(participates)
    p_topn            : dict[k -> ndarray] – P(finish top-k)
    p_topn_given_play : dict[k -> ndarray] – P(finish top-k | participates)
    """
    rng   = np.random.default_rng(seed)
    n     = len(players)
    max_N = max(n_values)

    plays_ct = np.zeros(n, dtype=np.int64)
    topn_ct  = {k: np.zeros(n, dtype=np.int64) for k in n_values}

    for start in range(0, n_sims, chunk):
        c = min(chunk, n_sims - start)

        scores = np.empty((c, n), dtype=np.float64)
        for i in range(n):
            idx = rng.choice(len(hist_composites[i]), size=c,
                             p=hist_wts[i], replace=True)
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

    with np.errstate(invalid='ignore', divide='ignore'):
        p_topn_given_play = {
            k: np.where(plays_ct > 0, topn_ct[k] / plays_ct, 0.0)
            for k in n_values
        }
    return (
        plays_ct / n_sims,
        {k: topn_ct[k] / n_sims for k in n_values},
        p_topn_given_play,
    )


# ── Main backtest loop ────────────────────────────────────────────────────────

def run_backtest(
    start_date: str   = START_DATE,
    n_sims: int       = N_SIMS,
    n_values: list    = N_VALUES,
    chunk: int        = CHUNK,
    base_seed: int    = BASE_SEED,
    output_path: str  = OUTPUT_PATH,
    sample: int       = None,     # if set, only process first N tournaments
) -> pd.DataFrame:

    t0 = time.time()

    print('Loading titled_tuesday_standings.csv ...')
    df_full = pd.read_csv('data/titled_tuesday_standings.csv', parse_dates=['date'])
    df_full = (
        df_full.sort_values('rank')
        .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
        .reset_index(drop=True)
    )
    print(f'  {len(df_full):,} rows | {df_full["username"].nunique():,} unique players '
          f'| {df_full["date"].min().date()} – {df_full["date"].max().date()}')

    print('Precomputing per-user histories ...')
    user_data = _precompute_user_histories(df_full)
    print(f'  {len(user_data):,} users indexed')

    bt_tournaments = (
        df_full.loc[df_full['date'] >= pd.Timestamp(start_date),
                    ['date', 'tournament_slug']]
        .drop_duplicates()
        .sort_values(['date', 'tournament_slug'])
        .reset_index(drop=True)
    )
    if sample:
        bt_tournaments = bt_tournaments.head(sample)
    n_total = len(bt_tournaments)
    print(f'  {n_total} tournaments to evaluate '
          f'({bt_tournaments["date"].min().date()} – '
          f'{bt_tournaments["date"].max().date()})\n')

    records = []
    for i, t_row in bt_tournaments.iterrows():
        t_date = t_row['date']
        t_slug = t_row['tournament_slug']

        players, p_play, hist_composites, hist_wts = _build_pool_at(user_data, t_date)

        if len(players) < max(n_values):
            print(f'  [{i+1:>3}/{n_total}] SKIP {t_slug}: pool={len(players)} < {max(n_values)}')
            continue

        p_sim_part, p_topn, p_topn_gp = _simulate(
            players, p_play, hist_composites, hist_wts,
            n_sims=n_sims, n_values=n_values, chunk=chunk, seed=base_seed + i,
        )
        player_idx  = {u: j for j, u in enumerate(players)}
        df_t        = df_full[df_full['tournament_slug'] == t_slug]
        field_size  = len(df_t)

        for _, r in df_t.iterrows():
            username = r['username']
            j        = player_idx.get(username, -1)
            rec = {
                'tournament_slug': t_slug,
                'date':            t_date,
                'username':        username,
                'rank':            int(r['rank']),
                'field_size':      field_size,
                'p_participate':   round(float(p_sim_part[j]), 6) if j >= 0 else np.nan,
            }
            for k in n_values:
                rec[f'p_top{k}']           = round(float(p_topn[k][j]),    6) if j >= 0 else np.nan
                rec[f'p_top{k}_given_play'] = round(float(p_topn_gp[k][j]), 6) if j >= 0 else np.nan
                rec[f'in_top{k}']          = int(r['rank'] <= k)
            records.append(rec)

        print(f'  [{i+1:>3}/{n_total}]  {t_slug}'
              f'  field={field_size:>3}  pool={len(players):>4}'
              f'  no_hist={sum(1 for r in df_t["username"] if r not in player_idx):>2}'
              f'  {time.time()-t0:>6.1f}s')

    df_out = pd.DataFrame(records)
    df_out.to_csv(output_path, index=False)
    print(f'\nSaved {len(df_out):,} prediction rows to {output_path}')
    print(f'Total time: {time.time()-t0:.1f}s')
    return df_out


# ── Quick calibration summary ─────────────────────────────────────────────────

def calibration_summary(df: pd.DataFrame, n_values: list = N_VALUES):
    """
    For each top-N threshold, print mean predicted probability vs actual hit rate.
    A well-calibrated model should show mean_pred ≈ hit_rate in every slice.
    """
    print('\n-- Global calibration (mean predicted vs actual hit rate) ----------')
    print(f'  {"Metric":<22}  {"mean_pred":>10}  {"hit_rate":>10}  {"n_bets":>8}  {"bias_pp":>8}')
    for k in n_values:
        col_p = f'p_top{k}_given_play'
        col_y = f'in_top{k}'
        sub = df[[col_p, col_y]].dropna()
        if sub.empty:
            continue
        mean_pred = sub[col_p].mean()
        hit_rate  = sub[col_y].mean()
        print(f'  top-{k:<18}  {mean_pred:>10.4f}  {hit_rate:>10.4f}'
              f'  {len(sub):>8,}  {(mean_pred - hit_rate)*100:>+7.2f}pp')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Score/tiebreak backtest')
    parser.add_argument('--start',   default=START_DATE,  help='First tournament date')
    parser.add_argument('--sims',    type=int, default=N_SIMS, help='Sims per tournament')
    parser.add_argument('--sample',  type=int, default=None,   help='Only run first N events')
    parser.add_argument('--output',  default=OUTPUT_PATH,       help='Output CSV path')
    args = parser.parse_args()

    df_results = run_backtest(
        start_date=args.start,
        n_sims=args.sims,
        output_path=args.output,
        sample=args.sample,
    )
    calibration_summary(df_results)
