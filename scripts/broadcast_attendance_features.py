#!/usr/bin/env python3
"""Measure how concurrent OTB tournaments affect Titled Tuesday attendance.

Two feature sets:
  EVENT-LEVEL  (same signal for every player on a given TT date):
    n_elite_named        named elite events running within ±7 days of TT
    n_high_prestige      high pct_titled OTB events running within ±7 days
    n_rounds_tuesday     broadcast rounds happening on TT calendar day
    elite_starts_within14  any named elite event begins within 14 days

  PLAYER-LEVEL (requires PGN-name → chess.com username matching):
    player_in_elite        player is in a named elite event ±7 days
    player_in_prestige     player is in a high-prestige OTB event ±7 days
    player_round_tuesday   player has a broadcast round on TT day

Analysis:
  1. Descriptive: attendance rate when each feature is 0 vs non-zero.
  2. Logistic regression: does adding broadcast features improve on
     the player's own rolling baseline attendance rate?

Usage:
    python scripts/broadcast_attendance_features.py
    python scripts/broadcast_attendance_features.py --min-appearances 5 --since 2020-01-01
"""

import argparse
import re
import sys
import unicodedata
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import DB_PATH

# ── Elite / prestige definitions ──────────────────────────────────────────────

ELITE_PATTERNS = [
    r'\bcandidates\b',
    r'world chess championship',
    r'chess olympiad',
    r'\bworld cup\b',
    r'grand swiss',
    r'sinquefield',
    r'norway chess',
    r'tata steel',
    r'\bgrand prix\b',
    r'champions chess tour',
    r'\bfide circuit\b',
    r'\bisle of man\b',
    r'\bwijk\b',           # Wijk aan Zee = Tata Steel
]

# Subset used for the "preparation" (starts-soon) feature
SUPERELITE_PATTERNS = [
    r'\bcandidates\b',
    r'world chess championship',
    r'chess olympiad',
    r'\bworld cup\b',
    r'grand swiss',
    r'sinquefield',
    r'norway chess',
    r'tata steel',
]

def _match(name: str, patterns) -> bool:
    nl = (name or '').lower()
    return any(re.search(p, nl) for p in patterns)


# ── Name normalisation ────────────────────────────────────────────────────────

def norm_name(s: str) -> str:
    """'Last, First' → 'first last', strip diacritics, lowercase."""
    if not s:
        return ''
    if ',' in s:
        last, first = s.split(',', 1)
        s = f"{first.strip()} {last.strip()}"
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return ' '.join(s.lower().split())


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(since: str):
    conn = sqlite3.connect(DB_PATH)

    df_tt = pd.read_sql_query(
        f"SELECT tournament_slug, date, username FROM titled_tuesday_standings WHERE date >= '{since}'",
        conn, parse_dates=['date'],
    )

    df_events = pd.read_sql_query(
        "SELECT broadcast_name, first_game_utc, last_game_utc, is_online, "
        "pct_titled, pct_with_fide_id, n_players FROM other_events",
        conn, parse_dates=['first_game_utc', 'last_game_utc'],
    )

    df_rounds = pd.read_sql_query(
        "SELECT broadcast_name, earliest_start_utc FROM other_event_rounds",
        conn, parse_dates=['earliest_start_utc'],
    )

    df_parts = pd.read_sql_query(
        "SELECT broadcast_name, player_name FROM other_event_participants",
        conn,
    )

    df_players = pd.read_sql_query(
        "SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL",
        conn,
    )

    conn.close()
    return df_tt, df_events, df_rounds, df_parts, df_players


# ── Event classification ──────────────────────────────────────────────────────

def classify_events(df_events: pd.DataFrame) -> pd.DataFrame:
    df = df_events.copy()
    df['is_elite_named']    = df['broadcast_name'].apply(lambda n: _match(n, ELITE_PATTERNS))
    df['is_superelite']     = df['broadcast_name'].apply(lambda n: _match(n, SUPERELITE_PATTERNS))
    df['is_high_prestige']  = (
        (df['is_online'] == 0) &
        (df['pct_titled'] >= 0.7) &
        (df['pct_with_fide_id'] >= 0.3) &
        (df['n_players'] >= 6)
    )
    # Ensure UTC-aware timestamps for comparisons
    for col in ['first_game_utc', 'last_game_utc']:
        if df[col].dt.tz is None:
            df[col] = df[col].dt.tz_localize('UTC')
    return df


# ── Player name matching ──────────────────────────────────────────────────────

def build_player_lookup(df_players: pd.DataFrame) -> dict:
    return {norm_name(row.player_name): row.username
            for row in df_players.itertuples()
            if row.player_name}


def match_participants(df_parts: pd.DataFrame, lookup: dict) -> pd.DataFrame:
    df = df_parts.copy()
    df['norm'] = df['player_name'].apply(norm_name)
    df['username'] = df['norm'].map(lookup)
    matched = df['username'].notna().mean()
    print(f"  Name match rate: {matched:.1%} ({df['username'].notna().sum():,} / {len(df):,} participant rows)")
    return df[df['username'].notna()][['broadcast_name', 'username']].drop_duplicates()


# ── Feature computation ───────────────────────────────────────────────────────

def compute_event_level_features(tt_dates_utc: pd.DatetimeIndex,
                                  df_events: pd.DataFrame,
                                  df_rounds: pd.DataFrame) -> pd.DataFrame:
    """One row per TT date with event-level features."""
    print("  Computing event-level features...")
    rows = []
    rnd_dates = df_rounds['earliest_start_utc'].dt.normalize()

    for tt_ts in tt_dates_utc:
        w_start = tt_ts - pd.Timedelta(days=7)
        w_end   = tt_ts + pd.Timedelta(days=7)
        tt_day  = tt_ts.normalize()

        # Events overlapping ±7 day window
        overlap = df_events[
            (df_events['first_game_utc'] <= w_end) &
            (df_events['last_game_utc']  >= w_start)
        ]
        # Named elite events starting within 14 days (preparation)
        starts_soon = df_events[
            (df_events['first_game_utc'] >= tt_ts) &
            (df_events['first_game_utc'] <= tt_ts + pd.Timedelta(days=14)) &
            df_events['is_superelite']
        ]

        rows.append({
            'date': tt_ts.normalize().tz_localize(None),
            'n_elite_named':        int(overlap['is_elite_named'].sum()),
            'n_high_prestige':      int(overlap['is_high_prestige'].sum()),
            'n_rounds_tuesday':     int((rnd_dates == tt_day).sum()),
            'elite_starts_within14': int(len(starts_soon) > 0),
        })

    return pd.DataFrame(rows)


def compute_player_level_features(tt_dates_utc: pd.DatetimeIndex,
                                   df_events: pd.DataFrame,
                                   df_rounds: pd.DataFrame,
                                   df_parts_matched: pd.DataFrame) -> pd.DataFrame:
    """One row per (TT date, username) for players matched to broadcast events."""
    print("  Computing player-level features (this may take a moment)...")

    # Join participants with event metadata once
    df_pe = df_parts_matched.merge(
        df_events[['broadcast_name', 'first_game_utc', 'last_game_utc',
                   'is_elite_named', 'is_high_prestige']],
        on='broadcast_name',
    )

    # Join rounds with participants for same-day check
    df_round_player = df_rounds.merge(
        df_parts_matched, on='broadcast_name',
    )
    df_round_player['round_day'] = df_round_player['earliest_start_utc'].dt.normalize()

    all_rows = []
    for tt_ts in tt_dates_utc:
        w_start = tt_ts - pd.Timedelta(days=7)
        w_end   = tt_ts + pd.Timedelta(days=7)
        tt_day  = tt_ts.normalize()

        # Players in concurrent events
        concurrent = df_pe[
            (df_pe['first_game_utc'] <= w_end) &
            (df_pe['last_game_utc']  >= w_start)
        ]
        per_player = concurrent.groupby('username').agg(
            player_in_elite    = ('is_elite_named',   'any'),
            player_in_prestige = ('is_high_prestige', 'any'),
        ).reset_index()
        per_player['player_in_elite']    = per_player['player_in_elite'].astype(int)
        per_player['player_in_prestige'] = per_player['player_in_prestige'].astype(int)

        # Players with a round on this specific Tuesday
        same_day = df_round_player[df_round_player['round_day'] == tt_day]
        same_day_users = set(same_day['username'])
        per_player['player_round_tuesday'] = per_player['username'].isin(same_day_users).astype(int)

        per_player['date'] = tt_ts.normalize().tz_localize(None)
        all_rows.append(per_player)

    return pd.concat(all_rows, ignore_index=True)


# ── Rolling baseline attendance ───────────────────────────────────────────────

def compute_rolling_baseline(df_tt: pd.DataFrame, min_appearances: int) -> pd.DataFrame:
    """For each (player, TT date), compute fraction of prior TTs attended."""
    print("  Computing rolling baseline attendance...")
    df = df_tt.sort_values(['username', 'date']).copy()

    # All TT tournaments (even if player didn't attend) — use unique tournament dates
    all_dates = df['date'].sort_values().unique()
    career = df.groupby('username')['date'].agg(['min', 'max', 'count']).reset_index()
    career.columns = ['username', 'first_tt', 'last_tt', 'n_appearances']
    active = career[career['n_appearances'] >= min_appearances]

    # Build the eligible pool: (username, date) for active players
    # where date is between first_tt and last_tt
    rows = []
    for _, row in active.iterrows():
        u = row['username']
        eligible_dates = all_dates[
            (all_dates >= row['first_tt']) & (all_dates <= row['last_tt'])
        ]
        for d in eligible_dates:
            rows.append({'username': u, 'date': d})

    df_pool = pd.DataFrame(rows)

    # Mark who actually attended
    attended = df[['username', 'date']].copy()
    attended['participated'] = 1
    df_pool = df_pool.merge(attended, on=['username', 'date'], how='left')
    df_pool['participated'] = df_pool['participated'].fillna(0).astype(int)

    # Rolling baseline: fraction of prior TTs attended
    df_pool = df_pool.sort_values(['username', 'date'])
    df_pool['cumsum'] = df_pool.groupby('username')['participated'].cumsum()
    df_pool['cumcount'] = df_pool.groupby('username').cumcount() + 1
    # Use lag (exclude current row) to avoid data leakage
    df_pool['prior_attended'] = df_pool['cumsum'] - df_pool['participated']
    df_pool['prior_total'] = df_pool['cumcount'] - 1
    df_pool['baseline_rate'] = np.where(
        df_pool['prior_total'] >= 3,
        df_pool['prior_attended'] / df_pool['prior_total'],
        np.nan,  # too few prior obs
    )
    return df_pool.dropna(subset=['baseline_rate'])


# ── Analysis ──────────────────────────────────────────────────────────────────

def descriptive_analysis(df: pd.DataFrame, feature_cols: list):
    print("\n" + "="*65)
    print("DESCRIPTIVE: Attendance rate by feature value")
    print("="*65)
    overall = df['participated'].mean()
    print(f"Overall attendance rate: {overall:.3f}  (n={len(df):,})\n")
    print(f"  {'Feature':<30} {'=0 rate':>8}  {'n=0':>7}  {'>=1 rate':>8}  {'n>=1':>7}  {'delta':>7}")
    print("  " + "-"*62)
    for col in feature_cols:
        g0 = df[df[col] == 0]
        g1 = df[df[col] >= 1]
        if g1.empty:
            continue
        r0 = g0['participated'].mean()
        r1 = g1['participated'].mean()
        delta = r1 - r0
        print(f"  {col:<30} {r0:>8.3f}  {len(g0):>7,}  {r1:>8.3f}  {len(g1):>7,}  {delta:>+7.3f}")


def logistic_comparison(df: pd.DataFrame, event_feature_cols: list, player_feature_cols: list):
    print("\n" + "="*65)
    print("LOGISTIC REGRESSION: Cross-validated AUC (5-fold stratified)")
    print("="*65)

    df = df.dropna().copy()
    # Clip baseline to avoid ±inf in logit
    eps = 0.01
    df['logit_baseline'] = np.log(
        (df['baseline_rate'].clip(eps, 1-eps)) /
        (1 - df['baseline_rate'].clip(eps, 1-eps))
    )
    y = df['participated'].values

    def auc(X):
        pipe = Pipeline([
            ('scl', StandardScaler()),
            ('lr',  LogisticRegression(max_iter=500, C=1.0)),
        ])
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        scores = cross_val_score(pipe, X, y, cv=cv, scoring='roc_auc')
        return scores.mean(), scores.std()

    X_base = df[['logit_baseline']].values
    mu0, sd0 = auc(X_base)
    print(f"  Baseline only (rolling avg rate):         AUC = {mu0:.4f} ± {sd0:.4f}")

    X_ev = df[['logit_baseline'] + event_feature_cols].values
    mu1, sd1 = auc(X_ev)
    print(f"  + Event-level features:                   AUC = {mu1:.4f} ± {sd1:.4f}  ({mu1-mu0:+.4f})")

    all_feats = event_feature_cols + [c for c in player_feature_cols if c in df.columns]
    if any(c in df.columns for c in player_feature_cols):
        X_full = df[['logit_baseline'] + all_feats].values
        mu2, sd2 = auc(X_full)
        print(f"  + Player-level features:                  AUC = {mu2:.4f} ± {sd2:.4f}  ({mu2-mu0:+.4f})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--since',           default='2020-01-01',
                    help='Ignore TT dates before this (broadcast data starts 2020-01)')
    ap.add_argument('--min-appearances', type=int, default=8,
                    help='Min TT appearances for a player to be in the eligible pool')
    args = ap.parse_args()

    print(f"Loading data (since={args.since}, min_appearances={args.min_appearances})...")
    df_tt, df_events, df_rounds, df_parts, df_players = load_data(args.since)
    print(f"  TT rows: {len(df_tt):,}   TT dates: {df_tt['date'].nunique()}")
    print(f"  Broadcast events: {len(df_events):,}   rounds: {len(df_rounds):,}")

    print("Classifying events...")
    df_events = classify_events(df_events)
    n_elite   = df_events['is_elite_named'].sum()
    n_prest   = df_events['is_high_prestige'].sum()
    print(f"  Elite named: {n_elite}   High prestige OTB: {n_prest}")

    print("Matching broadcast participants to TT players...")
    lookup = build_player_lookup(df_players)
    df_parts_matched = match_participants(df_parts, lookup)

    # Ensure UTC-aware dates for comparison
    df_rounds['earliest_start_utc'] = pd.to_datetime(
        df_rounds['earliest_start_utc'], utc=True)

    tt_dates_utc = pd.DatetimeIndex(
        pd.to_datetime(df_tt['date'].unique())
    ).tz_localize('UTC')

    print("Computing features...")
    df_ev_feat = compute_event_level_features(tt_dates_utc, df_events, df_rounds)
    df_pl_feat = compute_player_level_features(tt_dates_utc, df_events, df_rounds, df_parts_matched)

    print("Building eligible player pool with rolling baseline...")
    df_pool = compute_rolling_baseline(df_tt, min_appearances=args.min_appearances)
    print(f"  Pool: {len(df_pool):,} (player, date) pairs  "
          f"({df_pool['username'].nunique():,} players × {df_pool['date'].nunique()} dates)")

    # Merge event-level features
    df_pool['date'] = pd.to_datetime(df_pool['date'])
    df_ev_feat['date'] = pd.to_datetime(df_ev_feat['date'])
    df_pool = df_pool.merge(df_ev_feat, on='date', how='left')

    # Merge player-level features (left join — only matched players get values)
    df_pl_feat['date'] = pd.to_datetime(df_pl_feat['date'])
    df_pool = df_pool.merge(df_pl_feat, on=['username', 'date'], how='left')
    for col in ['player_in_elite', 'player_in_prestige', 'player_round_tuesday']:
        df_pool[col] = df_pool[col].fillna(0).astype(int)

    event_cols  = ['n_elite_named', 'n_high_prestige', 'n_rounds_tuesday', 'elite_starts_within14']
    player_cols = ['player_in_elite', 'player_in_prestige', 'player_round_tuesday']

    descriptive_analysis(df_pool, event_cols + player_cols)
    logistic_comparison(df_pool, event_cols, player_cols)

    print(f"\nPlayer-level feature coverage: "
          f"{(df_pool['player_in_elite'] | df_pool['player_in_prestige'] | df_pool['player_round_tuesday']).mean():.1%} "
          f"of pool rows have at least one player-level feature set")


if __name__ == '__main__':
    main()
