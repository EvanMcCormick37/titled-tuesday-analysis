#!/usr/bin/env python3
"""
Generate official MC model predictions for the next Titled Tuesday.

Attendance adjustments (cuts, conflict caps, nudges, overrides) are now
stored in the attendance_adjustments DB table.  Populate or update that
table before running this script:

    python scripts/migrate_adjustments.py      # one-time seed
    # or manage rows from notebooks/command-center.ipynb

Usage
-----
    python scripts/make_predictions.py
    python scripts/make_predictions.py --date 2026-08-12
"""

import argparse
import sys
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings('ignore')

from src.pipeline import run_predictions, save_predictions, next_tourn_date


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='Tournament date to predict (YYYY-MM-DD). '
                        'Defaults to one week after the latest data in DB.')
    args = parser.parse_args()

    tourn_date = args.date or next_tourn_date()
    run = run_predictions(tourn_date)
    save_predictions(run, tourn_date)


if __name__ == '__main__':
    main()
