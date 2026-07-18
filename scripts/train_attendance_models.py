#!/usr/bin/env python3
"""
Train and evaluate attendance models for Titled Tuesday.

Trains 3 classifiers predicting P(player attends next tournament week)
on post-DATA_START data. Saves trained models to models/.

Usage
-----
    python scripts/train_attendance_models.py           # train and save
    python scripts/train_attendance_models.py --predict # predict next week
    python scripts/train_attendance_models.py --predict --date 2026-07-22 --top 50
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.attendance import (
    load_data, get_weekly_slots, build_dataset,
    make_models, cross_validate, train_final, save_models, load_models,
    predict_attendance, print_feature_importances,
)
from src.config import MODELS_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--predict', action='store_true',
                        help='Load saved models and predict instead of training')
    parser.add_argument('--date',    type=str, default=None,
                        help='Target date YYYY-MM-DD (default: next Tuesday after last data)')
    parser.add_argument('--top',     type=int, default=30,
                        help='Print top N players (default: 30)')
    args = parser.parse_args()

    print('Loading data...')
    df = load_data()
    weekly_slots = get_weekly_slots(df)
    print(f'  {df["username"].nunique():,} players | '
          f'{df["date"].min().date()} to {df["date"].max().date()}')
    print(f'  {len(weekly_slots):,} tournament weeks')

    if args.date:
        target_date = pd.Timestamp(args.date)
    else:
        last_date  = df['date'].max()
        days_ahead = (1 - last_date.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        target_date = last_date + pd.Timedelta(days=days_ahead)

    if args.predict:
        print('\nLoading saved models...')
        models_payload = load_models()
        if not models_payload:
            print('No saved models found in models/. Run without --predict first.')
            return

        print(f'\nPredicting attendance for {target_date.date()}...')
        results = predict_attendance(df, weekly_slots, models_payload, target_date)

        print(f'\nTop {args.top} by ensemble P(attend):')
        print(results.head(args.top).to_string(
            index=False, float_format=lambda x: f'{x:.3f}'
        ))

        out_path = MODELS_DIR / f'predictions_{target_date.date()}.csv'
        results.to_csv(out_path, index=False)
        print(f'\nFull predictions saved -> {out_path}')
        return

    # Train mode
    dataset_cache = MODELS_DIR / 'dataset_cache_post2025.parquet'
    MODELS_DIR.mkdir(exist_ok=True)
    if dataset_cache.exists():
        print('\nLoading cached training dataset...')
        dataset = pd.read_parquet(dataset_cache)
    else:
        print('\nBuilding training dataset...')
        dataset = build_dataset(df, weekly_slots)
        dataset.to_parquet(dataset_cache, index=False)
        print(f'  Cached to {dataset_cache}')

    n_pos = int(dataset['label'].sum())
    n_tot = len(dataset)
    print(f'  {n_tot:,} observations | '
          f'label=1: {n_pos:,} ({n_pos/n_tot:.1%}) | '
          f'label=0: {n_tot-n_pos:,} ({(n_tot-n_pos)/n_tot:.1%})')
    print(f'  {dataset["username"].nunique():,} unique players in dataset')

    print('\nCross-validating (TimeSeriesSplit, 4 folds)...')
    models     = make_models()
    cv_results = cross_validate(dataset, models)

    print('\nTraining final models on all data...')
    final_models = train_final(dataset, models)

    print('\nSaving models...')
    save_models(final_models, cv_results)

    print_feature_importances(final_models)

    print(f'\nNext prediction target: {target_date.date()}')
    print('Run with --predict to generate attendance probabilities.')
    print('\nDone.')


if __name__ == '__main__':
    main()
