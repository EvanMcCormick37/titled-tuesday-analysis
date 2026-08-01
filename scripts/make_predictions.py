#!/usr/bin/env python3
"""
Generate official MC model predictions for the next Titled Tuesday.

Attendance adjustments (cut_players, keep_players, p_participate_overrides)
are passed directly to run_predictions().  Edit this file or use
notebooks/command-center.ipynb to set them before running.

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

from src.pipeline import run_predictions, next_tourn_date


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='Tournament date to predict (YYYY-MM-DD). '
                        'Defaults to one week after the latest data in DB.')
    args = parser.parse_args()

    tourn_date = args.date or next_tourn_date()
    run_predictions(tourn_date, save_official=True)


if __name__ == '__main__':
    main()
