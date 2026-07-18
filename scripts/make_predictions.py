#!/usr/bin/env python3
"""
Generate official MC model predictions for the next Titled Tuesday.

Predicts P(top-N | plays) for N in (1, 3, 5, 8, 10), then writes the full
player set to latest_model_predictions, replacing the previous prediction set.

Usage
-----
    python scripts/make_predictions.py
"""

import json
import sqlite3
import sys
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings('ignore')

import pandas as pd
from src.config import DB_PATH, N_SIMS
from src.data import load_and_prepare
from src.simulation import build_player_pool_score, run_simulation_score, build_results

PRED_N_VALUES = [1, 3, 5, 8, 10]

CUT_PLAYERS = [
    'Matthias Bluebaum',
    'Levon Aronian',
    'Yagiz Kaan Erdogmus',
    'Jose Martinez',
    'Le Quang Liem',
    'Daniel Naroditsky',
    'Hans Niemann',
    'Arjun Erigaisi',
    'Pranesh Munirethinam',
    'Dmitry Andreikin',
    'Nihal Sarin',
    'Alireza Firouzja',
    'Nodirbek Abdusattorov',
]
KEEP_PLAYERS = []


def main():
    # Determine the tournament date being predicted
    conn = sqlite3.connect(DB_PATH)
    last_date = conn.execute(
        'SELECT MAX(date) FROM titled_tuesday_standings'
    ).fetchone()[0]
    conn.close()
    tourn_date = (pd.Timestamp(last_date) + pd.Timedelta(weeks=1)).date().isoformat()
    print(f'Predicting for tournament date: {tourn_date}')

    df, p_participate, app_counts = load_and_prepare(
        CUT_PLAYERS, KEEP_PLAYERS, attendance_model='decay'
    )
    players, p_play, hist_composites, hist_wts = build_player_pool_score(
        df, p_participate, app_counts, min_appearances=5
    )

    print(f'Pool: {len(players):,} players | simulating {N_SIMS:,} tournaments...')
    plays_ct, topn_ct = run_simulation_score(
        players, p_play, hist_composites, hist_wts,
        n_sims=N_SIMS, n_values=PRED_N_VALUES,
    )
    results = build_results(
        players, p_play, plays_ct, topn_ct,
        n_sims=N_SIMS, n_values=PRED_N_VALUES,
    )

    # Keep only p_participate and the conditional probabilities
    keep_cols = ['p_participate'] + [f'P_top{k}_given_play' for k in PRED_N_VALUES]
    results   = results[keep_cols].reset_index()

    results['tourn_date']   = tourn_date
    results['cut_players']  = json.dumps(CUT_PLAYERS)
    results['keep_players'] = json.dumps(KEEP_PLAYERS)

    conn = sqlite3.connect(DB_PATH)
    results.to_sql('latest_model_predictions', conn, if_exists='replace', index=False)
    conn.close()
    print(f'Saved {len(results):,} rows -> latest_model_predictions (tourn_date={tourn_date})')


if __name__ == '__main__':
    main()
