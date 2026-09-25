#!/usr/bin/env python3
"""Place resting YES/NO bids on Kalshi Titled Tuesday markets.

Reads latest_model_predictions from the DB, computes bids at
max(fair / markup, fair - max_discount), applies pullback if an ask is already
at or below the computed bid, and cancels stale open orders when the
new price is lower.

Usage
-----
    # Dry run (default — no orders placed)
    python scripts/place_bids.py --date 2026-08-04

    # Live — actually submit orders (1 contract each)
    python scripts/place_bids.py --date 2026-08-04 --live

    # Live with higher contract count and tighter markup
    python scripts/place_bids.py --date 2026-08-04 --live --count 5 --markup 1.25

    # Only place YES bids
    python scripts/place_bids.py --date 2026-08-04 --side yes
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kalshi_core import KalshiClient
from src.trading import place_bids
from src.config import TOURN_DATE


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date", default=TOURN_DATE,
        help="Tournament date in YYYY-MM-DD format (e.g. 2026-08-04)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually submit orders. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--count", type=int, default=200,
        help="Number of contracts per bid (default: 1)",
    )
    parser.add_argument(
        "--markup", type=float, default=1.5,
        help="Bid = fair / markup. Default 1.5 → bid = 2/3 fair.",
    )
    parser.add_argument(
        "--max-discount", type=float, default=15,
        help="Maximum discount from fair price in dollars (default: 0.10).",
    )
    parser.add_argument(
        "--side", choices=["yes", "no"], default=None,
        help="Restrict bids to only 'yes' or 'no' side. Omit for both.",
    )
    args = parser.parse_args()

    client = KalshiClient()
    result = place_bids(
        client,
        tourn_date=args.date,
        count=args.count,
        markup=args.markup,
        max_discount=args.max_discount,
        side_filter=args.side,
        dry_run=not args.live,
    )

    if result["unmatched"]:
        print(f"\nNote: {len(result['unmatched'])} player name(s) could not be matched.")
        print("Add aliases to player_information to resolve them.")


if __name__ == "__main__":
    main()
