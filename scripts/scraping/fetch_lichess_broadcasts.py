#!/usr/bin/env python3
"""Download Lichess broadcast monthly PGN dumps for a date range.

Files live at:
    https://database.lichess.org/broadcast/lichess_db_broadcast_YYYY-MM.pgn.zst

Usage:
    python scripts/fetch_lichess_broadcasts.py --start 2025-09 --end 2026-07 --out ./dumps
"""
import argparse
import sys
import urllib.request
from pathlib import Path

BASE = 'https://database.lichess.org/broadcast'


def month_range(start: str, end: str):
    y, m = map(int, start.split('-'))
    ye, me = map(int, end.split('-'))
    while (y, m) <= (ye, me):
        yield f'{y:04d}-{m:02d}'
        m += 1
        if m == 13:
            y, m = y + 1, 1


def fetch(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        print(f'  skip (exists): {dest.name}')
        return True
    tmp = dest.with_suffix(dest.suffix + '.part')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'tt-attendance-research'})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.rename(dest)
        print(f'  ok: {dest.name} ({dest.stat().st_size/1e6:.1f} MB)')
        return True
    except Exception as e:
        print(f'  FAILED {url}: {e}', file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--start', default='2014-10', help='first month YYYY-MM')
    ap.add_argument('--end',   default='2026-07', help='last month YYYY-MM')
    ap.add_argument('--out',   default='./dumps',  help='output directory')
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    months = list(month_range(args.start, args.end))
    print(f'Fetching {len(months)} monthly broadcast dumps -> {out}/')
    n_ok = 0
    for ym in months:
        name  = f'lichess_db_broadcast_{ym}.pgn.zst'
        n_ok += fetch(f'{BASE}/{name}', out / name)
    print(f'Done: {n_ok}/{len(months)} files present.')


if __name__ == '__main__':
    main()
