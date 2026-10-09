#!/usr/bin/env python3
"""Place resting NO bids at (P(top-N | plays) × 100 − 15)¢, expiring 10 min after start.

Strategy — fade retail overpayment of YES when a low-attendance star shows up.
Uses the CONDITIONAL-on-plays probability (not the unconditional one) because
the thesis assumes the player is already on the roster when the overpay happens.
Expiration default 11:10 AM ET on tournament day; round-1 results shouldn't
have moved the top-N odds meaningfully by then.

Usage
-----
    # Dry run
    python scripts/place_fade_bids.py --date 2026-10-07

    # Live
    python scripts/place_fade_bids.py --date 2026-10-07 --live

    # Different discount / count / count
    python scripts/place_fade_bids.py --date 2026-10-07 --live --discount 20 --count 100

    # Different post-start expiry window (e.g. 20 minutes after start)
    python scripts/place_fade_bids.py --date 2026-10-07 --live --minutes-after-start 20
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kalshi_core import KalshiClient
from src.config import TOURN_DATE
from src.trading import place_fade_no_bids


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--date', default=TOURN_DATE,
                    help='Tournament date YYYY-MM-DD (default: src.config.TOURN_DATE)')
    ap.add_argument('--live', action='store_true',
                    help='Actually place orders. Without this flag, runs in dry-run mode.')
    ap.add_argument('--discount', type=int, default=15,
                    help='Cents off fair NO price (default: 15)')
    ap.add_argument('--count', type=int, default=200,
                    help='Contracts per bid (default: 200)')
    ap.add_argument('--minutes-after-start', type=int, default=10,
                    help='Minutes after 11:00 ET start that bids expire (default: 10)')
    args = ap.parse_args()

    client = KalshiClient()
    result = place_fade_no_bids(
        client,
        tourn_date=args.date,
        discount_cents=args.discount,
        count=args.count,
        minutes_after_start=args.minutes_after_start,
        dry_run=not args.live,
    )

    if result['unmatched']:
        print(f"\nNote: {len(result['unmatched'])} player name(s) could not be matched.")
        print("Add aliases to player_information to resolve them.")


if __name__ == '__main__':
    main()
