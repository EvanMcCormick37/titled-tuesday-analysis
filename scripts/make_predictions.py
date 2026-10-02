#!/usr/bin/env python3
"""
Generate official MC model predictions for the next Titled Tuesday.

Reads attendance adjustments from the `manual_adjustments` DB table (populated
by the dashboard, Telegram bot, or notebook) and passes them to run_adjusted().

Usage
-----
    python scripts/make_predictions.py
    python scripts/make_predictions.py --date 2026-08-12
    python scripts/make_predictions.py --raw-only       # skip run_adjusted step
"""

import argparse
import sys
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings('ignore')

from src.pipeline import run_raw, run_adjusted, next_tourn_date, load_adjustments


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='Tournament date to predict (YYYY-MM-DD). '
                        'Defaults to one week after the latest data in DB.')
    parser.add_argument('--raw-only', action='store_true',
                        help='Only run run_raw (skip the adjusted re-simulation).')
    args = parser.parse_args()

    tourn_date = args.date or next_tourn_date()
    raw_run = run_raw(tourn_date, save_to_db=True)
    if args.raw_only:
        return

    adj = load_adjustments(tourn_date)
    print(f'  Loaded adjustments from DB: '
          f'{len(adj["cut_players"])} cut, {len(adj["keep_players"])} keep, '
          f'{len(adj["p_participate_overrides"])} overrides, '
          f'{len(adj["p_nudges"])} nudges, global_nudge={adj["global_nudge"]:+.2f}')
    run_adjusted(tourn_date, raw_run=raw_run, save_official=True, **adj)


if __name__ == '__main__':
    main()
