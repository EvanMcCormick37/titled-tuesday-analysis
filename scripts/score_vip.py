#!/usr/bin/env python3
"""VIP Brier score for backtest runs: accuracy on the top players actually traded.

Scores each run's models on the cohort in src/vip_players.py, against a baseline
of each player's own top-N rate over the previous year. Prints the Brier table,
price and attendance calibration, and bootstrap ranges (over events) for each
model against the baseline and against --reference.

Runs against the local SQLite snapshot and data/backtests.db.

Usage:
    python scripts/score_vip.py 17
    python scripts/score_vip.py 17 18 --reference score_smooth_narrow
    python scripts/score_vip.py 17 --drop-conflicts    # leave out week-of conflicted VIPs
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src import backtest_runs as br
from src.config import DB_PATH
from src.markets import SPECS
from src.vip_players import resolve


def _standings(conn, spec) -> pd.DataFrame:
    played = f' WHERE {spec.model.played_filter}' if spec.model.played_filter else ''
    return pd.read_sql_query(
        f'SELECT substr(CAST(date AS TEXT), 1, 10) AS d, username, rank FROM {spec.model.standings_table}{played}',
        conn)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs', nargs='+', type=int)
    ap.add_argument('--reference', help='model to compare every other model with (default: the first)')
    ap.add_argument('--drop-conflicts', action='store_true',
                    help="leave out (event, VIP) pairs in attendance_conflicts, as week-of cuts would")
    args = ap.parse_args()

    runs = br.list_runs()
    markets = {runs.loc[r, 'market'] for r in args.runs}
    if len(markets) != 1:
        raise SystemExit(f'Runs span several markets: {markets}')
    spec = SPECS[markets.pop()]

    conn = sqlite3.connect(DB_PATH)
    accounts, unmatched = resolve(conn)
    standings = _standings(conn, spec)
    frame = br.vip_frame(br.load_predictions(args.runs), standings, accounts)
    print(f'{spec.label}: {frame["player"].nunique()} VIPs; no account on record: {unmatched}')

    if args.drop_conflicts:
        conflicts = pd.read_sql_query(
            'SELECT substr(CAST(date AS TEXT), 1, 10) AS d, username FROM attendance_conflicts WHERE market = ?',
            conn, params=(spec.slug,))
        last = conflicts['d'].max()
        pairs = {(d, accounts[u]) for d, u in zip(conflicts['d'], conflicts['username']) if u in accounts}
        keep = [(d, p) not in pairs for d, p in zip(frame['tourn_date'], frame['player'])]
        frame = frame[keep & (frame['tourn_date'] <= last)]
        print(f'Dropped week-of conflicts; events up to {last} (the end of the conflict data)')

    pd.set_option('display.width', 220)
    print('\nVIP Brier (lower is better; skill > 0 beats the baseline)')
    print(br.vip_summary(frame).round(4).to_string())

    cal = br.vip_calibration(frame)
    print('\nPrice calibration, predicted / observed (count)')
    for (run, model), g in cal.groupby(['run_id', 'model']):
        print(f'  {run} {model:<28}' + '  '.join(
            f'{b.left:.2f}-{b.right:.2f}: {r.predicted:.3f}/{r.observed:.3f} ({r["count"]})'
            for b, (_, r) in zip(g['bin'], g.iterrows())))
    att = br.vip_attendance(frame)
    print('\nAttendance calibration, predicted / observed (count)')
    for (run, model), g in att.groupby(['run_id', 'model']):
        print(f'  {run} {model:<28}' + '  '.join(
            f'{b.left:.2f}-{b.right:.2f}: {r.predicted:.3f}/{r.observed:.3f} ({r["count"]})'
            for b, (_, r) in zip(g['bin'], g.iterrows())))

    pairs = list(frame[['run_id', 'model']].drop_duplicates().itertuples(index=False))
    ref = next((p for p in pairs if p.model == args.reference), pairs[0])
    print(f'\nBrier change, 95% range over events (below 0 = better); reference {ref.run_id} {ref.model}')
    for p in pairs:
        lo, mid, hi = br.vip_bootstrap(frame, p.model, 'baseline', p.run_id)
        line = f'  {p.run_id} {p.model:<28} vs baseline {mid:+.2%} [{lo:+.2%}, {hi:+.2%}]'
        if p != ref:
            lo, mid, hi = br.vip_bootstrap(frame, p.model, ref.model, p.run_id, ref.run_id)
            line += f'   vs reference {mid:+.2%} [{lo:+.2%}, {hi:+.2%}]'
        print(line)


if __name__ == '__main__':
    main()
