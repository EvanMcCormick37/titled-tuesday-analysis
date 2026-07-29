#!/usr/bin/env python3
"""
Backtest P&L: simulated returns from betting on model edge in historical Kalshi markets.

For each settled Kalshi market snapshot that overlaps with walk-forward backtest
predictions, identifies YES positions where model probability exceeds the effective
ask price by a threshold multiple, and NO positions where the bid price exceeds
the model probability by the same multiple.  Assumes all available contracts
(open_interest) are taken, and tallies actual cost and return.

Usage
-----
    python scripts/backtest_pnl.py
    python scripts/backtest_pnl.py --threshold 1.20
    python scripts/backtest_pnl.py --threshold 1.25 --by-tournament
    python scripts/backtest_pnl.py --threshold 1.25 --verbose
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.config import DB_PATH
from src.portfolio import kalshi_order_price

# Maps parsed N → backtest_predictions column for P(top-N | plays)
P_COL = {
    1:  'P_top1_given_play',
    3:  'P_top3_given_play',
    5:  'P_top5_given_play',
    8:  'P_top8_given_play',
    10: 'P_top10_given_play',
}

_TICKER_N_RE = re.compile(r'T(\d+)$')


def _parse_n(event_ticker: str) -> int | None:
    """Extract N from KXTITLEDTUESTOP-...-T{N} tickers; winner markets → 1."""
    m = _TICKER_N_RE.search(event_ticker or '')
    if m:
        return int(m.group(1))
    return 1 if 'KXTITLEDTUESDAY-' in event_ticker else None


def load_data() -> pd.DataFrame:
    query = """
        SELECT
            kms.tournament_date,
            kms.event_ticker,
            kms.player,
            kms.yes_bid_close,
            kms.yes_ask_close,
            kms.open_interest,
            kms.settlement_value,
            bp.p_participate,
            bp.P_top1_given_play,
            bp.P_top3_given_play,
            bp.P_top5_given_play,
            bp.P_top8_given_play,
            bp.P_top10_given_play
        FROM kalshi_market_snapshots kms
        INNER JOIN player_information pi
            ON kms.player = pi.player_name
           AND pi.is_default = 1
        INNER JOIN backtest_predictions bp
            ON kms.tournament_date = bp.tourn_date
           AND pi.username = bp.username
        WHERE kms.open_interest > 0
          AND kms.yes_ask_close IS NOT NULL
          AND kms.yes_bid_close IS NOT NULL
          AND kms.settlement_value IS NOT NULL
    """
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(query, conn)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['n'] = df['event_ticker'].map(_parse_n)
    df['model_prob'] = pd.NA
    for n, col in P_COL.items():
        mask = df['n'] == n
        if mask.any():
            df.loc[mask, 'model_prob'] = (
                df.loc[mask, 'p_participate'] * df.loc[mask, col]
            )
    df['yes_ask_eff'] = df['yes_ask_close'].map(kalshi_order_price)
    df['no_ask_eff']  = (1.0 - df['yes_bid_close']).map(kalshi_order_price)
    return df.dropna(subset=['model_prob'])


def run(threshold: float = 1.25, by_tournament: bool = False,
        verbose: bool = False) -> None:
    df = load_data()
    if df.empty:
        print('No matched market/prediction data found.')
        return

    df = prepare(df)
    n_markets   = len(df)
    n_dates     = df['tournament_date'].nunique()

    yes_mask = df['model_prob'] > df['yes_ask_eff'] * threshold
    no_mask  = df['yes_bid_close'] > df['model_prob'] * threshold

    df_yes = df[yes_mask].copy()
    df_no  = df[no_mask].copy()

    df_yes['cost']   = df_yes['yes_ask_eff'] * df_yes['open_interest']
    df_yes['return'] = df_yes['settlement_value'] * df_yes['open_interest']
    df_no['cost']    = df_no['no_ask_eff'] * df_no['open_interest']
    df_no['return']  = (1.0 - df_no['settlement_value']) * df_no['open_interest']

    yes_cost, yes_ret = df_yes['cost'].sum(), df_yes['return'].sum()
    no_cost,  no_ret  = df_no['cost'].sum(),  df_no['return'].sum()
    total_cost   = yes_cost + no_cost
    total_return = yes_ret + no_ret

    # ── Overall summary ────────────────────────────────────────────────────────
    print(f'Markets evaluated : {n_markets:,}  |  Tournaments: {n_dates}'
          f'  |  Threshold: {threshold:.2f}x')
    print()
    hdr = (f'{"":20} {"Bets":>6}  {"Cost ($)":>10}'
           f'  {"Return ($)":>10}  {"P&L ($)":>10}')
    sep = '-' * len(hdr)
    print(hdr)
    print(sep)
    print(f'{"YES positions":<20} {len(df_yes):>6}  '
          f'{yes_cost:>10.2f}  {yes_ret:>10.2f}  {yes_ret - yes_cost:>+10.2f}')
    print(f'{"NO positions":<20} {len(df_no):>6}  '
          f'{no_cost:>10.2f}  {no_ret:>10.2f}  {no_ret - no_cost:>+10.2f}')
    print(sep)
    print(f'{"Total":<20} {len(df_yes) + len(df_no):>6}  '
          f'{total_cost:>10.2f}  {total_return:>10.2f}  '
          f'{total_return - total_cost:>+10.2f}')

    # ── Per-tournament breakdown ───────────────────────────────────────────────
    if by_tournament:
        print('\n-- Per-tournament breakdown ------------------------------------------')
        hdr2 = (f'{"Date":<14} {"Bets":>5}  {"Cost ($)":>9}'
                f'  {"Return ($)":>10}  {"P&L ($)":>9}')
        print(hdr2)
        print('-' * len(hdr2))
        for date in sorted(df['tournament_date'].unique()):
            dy = df_yes[df_yes['tournament_date'] == date]
            dn = df_no[df_no['tournament_date'] == date]
            c  = dy['cost'].sum() + dn['cost'].sum()
            r  = dy['return'].sum() + dn['return'].sum()
            n  = len(dy) + len(dn)
            print(f'{date:<14} {n:>5}  {c:>9.2f}  {r:>10.2f}  {r - c:>+9.2f}')

    # ── Individual bets ────────────────────────────────────────────────────────
    if verbose:
        fmt = lambda x: f'{x:.4f}'
        print('\n-- YES positions -----------------------------------------------------')
        print(df_yes[['tournament_date', 'player', 'n', 'model_prob',
                       'yes_ask_eff', 'open_interest', 'settlement_value',
                       'cost', 'return']]
              .sort_values(['tournament_date', 'player'])
              .to_string(index=False, float_format=fmt))
        print('\n-- NO positions ------------------------------------------------------')
        print(df_no[['tournament_date', 'player', 'n', 'model_prob',
                     'yes_bid_close', 'open_interest', 'settlement_value',
                     'cost', 'return']]
              .sort_values(['tournament_date', 'player'])
              .to_string(index=False, float_format=fmt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--threshold', type=float, default=1.25,
        help='Edge multiple required to place a bet (default: %(default)s)',
    )
    parser.add_argument(
        '--by-tournament', action='store_true',
        help='Print per-tournament P&L breakdown',
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Print every individual bet',
    )
    args = parser.parse_args()
    run(threshold=args.threshold,
        by_tournament=args.by_tournament,
        verbose=args.verbose)
