#!/usr/bin/env python3
"""
Walk-forward backtest: score/tiebreak Monte Carlo model.

For each Titled Tuesday from START_DATE onward, uses all prior data as training
history and records predicted top-N probabilities for every player in the pool —
including players who did NOT participate (rank = -1, participated = 0).

Output CSV columns
------------------
tournament_slug, date, username, rank, participated, field_size,
p_participate,
p_top1, p_top3, p_top8, p_top10,
p_top1_given_play, p_top3_given_play, p_top8_given_play, p_top10_given_play,
in_top1, in_top3, in_top8, in_top10

attendance_model options
------------------------
  decay           – recency-weighted EWMA (default)
  gradient_boost  – GBM from src/attendance.py (post-2025-09-02 era only)
  random_forest   – RF  from src/attendance.py (post-2025-09-02 era only)
  logistic        – LR  from src/attendance.py (post-2025-09-02 era only)

Usage
-----
    python scripts/run_backtest.py                      # all 4 models
    python scripts/run_backtest.py --sample 20          # quick test, first 20 events
    python scripts/run_backtest.py --model decay --sample 5
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
    DB_PATH, BACKTEST_DIR,
    SKILL_DECAY, PARTICIPATION_DECAY, _SCORE_COMPOSITE_SCALE,
)

START_DATE        = '2025-09-02'
N_VALUES          = [1, 3, 8, 10]
N_SIMS            = 10_000
CHUNK             = 5_000
BASE_SEED         = 42
ML_ERA_START      = pd.Timestamp('2025-09-02')
ATTENDANCE_MODELS = ['decay', 'gradient_boost', 'random_forest', 'logistic']

_Z_PART = 1.0 / (1.0 - PARTICIPATION_DECAY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _precompute_user_histories(df: pd.DataFrame) -> dict:
    df = df.copy()
    df['composite'] = (
        df['score'].fillna(0).to_numpy(dtype=np.float64) * _SCORE_COMPOSITE_SCALE
        + df['tie_break'].fillna(0).to_numpy(dtype=np.float64)
    )
    user_data = {}
    for username, grp in df.groupby('username'):
        user_data[username] = {
            'dates':      grp['date'].to_numpy(),
            'composites': grp['composite'].to_numpy(dtype=np.float64),
        }
    return user_data


def _build_pool_at(user_data: dict, cutoff: pd.Timestamp):
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
        weeks_back = (days_back / 7.0).astype(int)
        skill_w    = SKILL_DECAY         ** weeks_back
        part_w     = PARTICIPATION_DECAY ** weeks_back

        p_part       = float(part_w.sum()) / _Z_PART
        skill_w_norm = skill_w / skill_w.sum()

        players.append(username)
        p_play_list.append(min(p_part, 1.0))
        hist_composites.append(data['composites'][mask])
        hist_wts.append(skill_w_norm)

    p_play = np.array(p_play_list, dtype=np.float64)
    return players, p_play, hist_composites, hist_wts


def _simulate(players, p_play, hist_composites, hist_wts,
              n_sims: int, n_values: list, chunk: int, seed: int):
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
    start_date: str       = START_DATE,
    n_sims: int           = N_SIMS,
    n_values: list        = N_VALUES,
    chunk: int            = CHUNK,
    base_seed: int        = BASE_SEED,
    output_path: str      = None,
    sample: int           = None,
    attendance_model: str = 'decay',
) -> pd.DataFrame:

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = BACKTEST_DIR / f'backtest_score_{attendance_model}.csv'

    t0 = time.time()

    ml_models = None
    am_df     = None
    am_slots  = None
    _get_ml_p = None

    if attendance_model != 'decay':
        from src.attendance import (
            load_data as _am_load, get_weekly_slots as _am_slots_fn,
            load_models as _am_load_models, get_p_participate_for_mc,
        )
        print(f'Loading attendance model: {attendance_model} ...')
        am_df     = _am_load()
        am_slots  = _am_slots_fn(am_df)
        ml_models = _am_load_models()
        _get_ml_p = get_p_participate_for_mc
        print(f'  Models loaded: {list(ml_models.keys())}')

    print('Loading standings from DB...')
    conn    = sqlite3.connect(DB_PATH)
    df_full = pd.read_sql_query('SELECT * FROM titled_tuesday_standings', conn, parse_dates=['date'])
    conn.close()
    df_full = (
        df_full.sort_values('rank')
        .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
        .reset_index(drop=True)
    )
    print(f'  {len(df_full):,} rows | {df_full["username"].nunique():,} unique players '
          f'| {df_full["date"].min().date()} – {df_full["date"].max().date()}')

    print('Precomputing per-user histories...')
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
          f'{bt_tournaments["date"].max().date()})')
    print(f'  attendance_model = {attendance_model}\n')

    # Reuse pre-era rows from decay CSV to save ~75% runtime for ML models
    pre_era_records = []
    pre_era_slugs   = set()

    if attendance_model != 'decay':
        decay_csv = BACKTEST_DIR / 'backtest_score_decay.csv'
        if decay_csv.exists():
            print(f'Pre-loading pre-era rows from {decay_csv} ...')
            _decay_df = pd.read_csv(decay_csv, parse_dates=['date'])
            _pre      = _decay_df[_decay_df['date'] < ML_ERA_START]
            pre_era_records = _pre.to_dict('records')
            pre_era_slugs   = set(_pre['tournament_slug'].unique())
            print(f'  Reusing {len(pre_era_records):,} rows '
                  f'from {len(pre_era_slugs)} pre-era tournaments\n')
        else:
            print(f'  (decay CSV not found at {decay_csv} — will simulate pre-era too)\n')

    records = []
    for i, t_row in bt_tournaments.iterrows():
        t_date = t_row['date']
        t_slug = t_row['tournament_slug']

        if t_slug in pre_era_slugs:
            continue

        players, p_play, hist_composites, hist_wts = _build_pool_at(user_data, t_date)

        if len(players) < max(n_values):
            print(f'  [{i+1:>3}/{n_total}] SKIP {t_slug}: pool={len(players)} < {max(n_values)}')
            continue

        ml_p = None
        if ml_models is not None and t_date >= ML_ERA_START:
            ml_p = _get_ml_p(am_df, am_slots, ml_models,
                             pd.Timestamp(t_date), model_name=attendance_model)
            player_to_j = {u: j for j, u in enumerate(players)}
            for username, prob in ml_p.items():
                if username in player_to_j:
                    p_play[player_to_j[username]] = float(prob)

        p_sim_part, p_topn, p_topn_gp = _simulate(
            players, p_play, hist_composites, hist_wts,
            n_sims=n_sims, n_values=n_values, chunk=chunk, seed=base_seed + i,
        )

        df_t         = df_full[df_full['tournament_slug'] == t_slug]
        actual_ranks = dict(zip(df_t['username'], df_t['rank'].astype(int)))
        field_size   = len(df_t)
        player_idx   = {u: j for j, u in enumerate(players)}
        output_users = set(players) | set(actual_ranks.keys())

        for username in output_users:
            j            = player_idx.get(username, -1)
            rank_val     = actual_ranks.get(username, -1)
            participated = int(rank_val != -1)

            if j >= 0:
                rec = {
                    'tournament_slug': t_slug,
                    'date':            t_date,
                    'username':        username,
                    'rank':            rank_val,
                    'participated':    participated,
                    'field_size':      field_size,
                    'p_participate':   round(float(p_sim_part[j]), 6),
                }
                for k in n_values:
                    rec[f'p_top{k}']           = round(float(p_topn[k][j]),    6)
                    rec[f'p_top{k}_given_play'] = round(float(p_topn_gp[k][j]), 6)
                    rec[f'in_top{k}']           = int(participated and rank_val <= k)
            else:
                rec = {
                    'tournament_slug': t_slug,
                    'date':            t_date,
                    'username':        username,
                    'rank':            rank_val,
                    'participated':    participated,
                    'field_size':      field_size,
                    'p_participate':   np.nan,
                }
                for k in n_values:
                    rec[f'p_top{k}']           = np.nan
                    rec[f'p_top{k}_given_play'] = np.nan
                    rec[f'in_top{k}']           = int(participated and rank_val <= k)
            records.append(rec)

        n_ml = int(ml_p.index.isin(players).sum()) if ml_p is not None else 0
        print(f'  [{i+1:>3}/{n_total}]  {t_slug}'
              f'  field={field_size:>3}  pool={len(players):>5}'
              f'  output={len(output_users):>5}'
              f'  ml_overrides={n_ml:>4}'
              f'  {time.time()-t0:>7.1f}s')

    df_out = pd.DataFrame(pre_era_records + records)
    df_out = df_out.sort_values(['date', 'tournament_slug', 'username']).reset_index(drop=True)
    df_out.to_csv(output_path, index=False)
    print(f'\nSaved {len(df_out):,} rows -> {output_path}')
    print(f'Total time: {time.time()-t0:.1f}s')
    return df_out


# ── Calibration summary ───────────────────────────────────────────────────────

def calibration_summary(df: pd.DataFrame, n_values: list = N_VALUES):
    sub = df[df['participated'] == 1].copy() if 'participated' in df.columns else df.copy()
    print('\n-- Attendance calibration (participants only) ----------------------')
    sub_att = df.dropna(subset=['p_participate'])
    if not sub_att.empty and 'participated' in df.columns:
        print(f'  mean p_participate:  {sub_att["p_participate"].mean():.4f}  '
              f'actual rate: {sub_att["participated"].mean():.4f}  '
              f'n={len(sub_att):,}')

    print('\n-- Rank-conditional calibration (mean predicted vs actual hit rate) -')
    print(f'  {"Metric":<22}  {"mean_pred":>10}  {"hit_rate":>10}  {"n_bets":>8}  {"bias_pp":>8}')
    for k in n_values:
        col_p = f'p_top{k}_given_play'
        col_y = f'in_top{k}'
        s = sub[[col_p, col_y]].dropna()
        if s.empty:
            continue
        mean_pred = s[col_p].mean()
        hit_rate  = s[col_y].mean()
        print(f'  top-{k:<18}  {mean_pred:>10.4f}  {hit_rate:>10.4f}'
              f'  {len(s):>8,}  {(mean_pred - hit_rate)*100:>+7.2f}pp')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start',  default=START_DATE)
    parser.add_argument('--sims',   type=int, default=N_SIMS)
    parser.add_argument('--sample', type=int, default=None,
                        help='Only run first N events (quick test)')
    parser.add_argument('--model',  default=None, choices=ATTENDANCE_MODELS,
                        help='Run a single model (default: all 4)')
    args = parser.parse_args()

    models_to_run = [args.model] if args.model else ATTENDANCE_MODELS

    for model in models_to_run:
        print(f'\n{"="*70}')
        print(f'  Running backtest: attendance_model = {model}')
        print(f'{"="*70}')
        df_results = run_backtest(
            start_date=args.start,
            n_sims=args.sims,
            sample=args.sample,
            attendance_model=model,
        )
        calibration_summary(df_results)
        print()
