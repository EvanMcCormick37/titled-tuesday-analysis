#!/usr/bin/env python3
"""
Copy latest_model_predictions -> historical_predictions.

Run this each Tuesday at 11 AM Eastern (after the model has produced its
official prediction for that week). Uses INSERT OR REPLACE so re-running
for the same tourn_date simply overwrites any prior snapshot for that week.

Usage
-----
    python scripts/save_weekly_predictions.py
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH


def main():
    conn = sqlite3.connect(DB_PATH)

    n_src = conn.execute('SELECT COUNT(*) FROM latest_model_predictions').fetchone()[0]
    if n_src == 0:
        print('latest_model_predictions is empty — nothing to save.')
        conn.close()
        return

    tourn_date = conn.execute(
        'SELECT DISTINCT tourn_date FROM latest_model_predictions LIMIT 1'
    ).fetchone()[0]

    conn.execute('''
        INSERT OR REPLACE INTO historical_predictions
            (tourn_date, username, p_participate,
             P_top1_given_play, P_top3_given_play, P_top5_given_play,
             P_top8_given_play, P_top10_given_play)
        SELECT
            tourn_date, username, p_participate,
            P_top1_given_play, P_top3_given_play, P_top5_given_play,
            P_top8_given_play, P_top10_given_play
        FROM latest_model_predictions
    ''')
    n_written = conn.execute(
        'SELECT COUNT(*) FROM historical_predictions WHERE tourn_date = ?', (tourn_date,)
    ).fetchone()[0]
    conn.commit()
    conn.close()

    print(f'Saved {n_written:,} rows to historical_predictions (tourn_date={tourn_date})')


if __name__ == '__main__':
    main()
