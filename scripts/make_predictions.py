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
import sys
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings('ignore')

import sqlite3
import pandas as pd
from src.config import DB_PATH, N_SIMS
from src.data import load_and_prepare
from src.simulation import build_player_pool_score, run_simulation_score, build_results

PRED_N_VALUES = [1, 3, 5, 8, 10]

# Players with a broadcast round on the next TT Tuesday (scheduling conflict).
# Names must match player_information.player_name exactly.
SCHEDULING_CONFLICT = [
    'Andrey Esipenko',
    'Zhamsaran Tsydypov',
    'David Paravyan',
    'Maxim Matlakov',
    'Arseniy Nesterov'
]
# Players with an unavoidable conflict; p_participate is set to 0.
CUT_PLAYERS = [
    'Hikaru Nakamura',
    'Fabiano Caruana',
    'Wesley So',
    'Levon Aronian',
    'Leinier Dominguez Perez',
    'Javokhir Sindarov',
    'Nodirbek Abdussatorov',
    'Nodirbek Yakubboev',
    'Shamsiddin Vokhidov',
    'Mukhiddin Madaminov',
    'Christopher Yoo',
    'Grigory Oparin',
]
KEEP_PLAYERS = []

# Closed/alt accounts → active canonical account.
CANONICAL_ACCOUNTS: dict[str, str] = {
    'IMHansNiemann':   'HansOnTwitch',
    'HansCoolNiemann': 'HansOnTwitch',
}

# All chess.com accounts belonging to the same player (for TT attendance aggregation).
PLAYER_ACCOUNT_GROUPS: dict[str, list[str]] = {
    'HansOnTwitch': ['HansOnTwitch', 'IMHansNiemann', 'HansCoolNiemann'],
}


def main():
    conn = sqlite3.connect(DB_PATH)
    last_date = conn.execute(
        'SELECT MAX(date) FROM titled_tuesday_standings'
    ).fetchone()[0]
    conn.close()
    tourn_date = (pd.Timestamp(last_date) + pd.Timedelta(weeks=1)).date().isoformat()
    print(f'Predicting for tournament date: {tourn_date}')

    df, p_participate, app_counts = load_and_prepare(
        scheduling_conflict=SCHEDULING_CONFLICT,
        cut_players=CUT_PLAYERS,
        keep_players=KEEP_PLAYERS,
        canonical_accounts=CANONICAL_ACCOUNTS,
        account_groups=PLAYER_ACCOUNT_GROUPS,
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
