#!/usr/bin/env python3
"""Place resting YES/NO bids on Kalshi Titled Tuesday markets.

Reads latest_model_predictions from the DB, computes bids at
max(fair / markup, fair - max_discount), applies pullback if an ask is already
at or below the computed bid, and cancels stale open orders when the
new price is lower.

Date is always resolved from `src.pipeline.next_tourn_date()` — the upcoming
TT is the only market Kalshi actually publishes before the previous week ends.

Usage
-----
    # Dry run (default — no orders placed)
    python scripts/place_bids.py

    # Live — actually submit orders
    python scripts/place_bids.py --live

    # Live with higher contract count and tighter markup
    python scripts/place_bids.py --live --count 5 --markup 1.25

    # Only place YES bids
    python scripts/place_bids.py --side yes
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kalshi_core import KalshiClient
from src.pipeline import next_tourn_date
from src.trading import place_bids


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually submit orders. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--count", type=int, default=200,
        help="Number of contracts per bid (default: 200)",
    )
    parser.add_argument(
        "--markup", type=float, default=1.5,
        help="Bid = fair / markup. Default 1.5 -> bid = 2/3 fair.",
    )
    parser.add_argument(
        "--max-discount", type=float, default=15,
        help="Maximum discount from fair price in cents (default: 15).",
    )
    parser.add_argument(
        "--side", choices=["yes", "no"], default=None,
        help="Restrict bids to only 'yes' or 'no' side. Omit for both.",
    )
    args = parser.parse_args()

    tourn_date = next_tourn_date()
    print(f"[place_bids] tourn_date = {tourn_date}")

    client = KalshiClient()
    result = place_bids(
        client,
        tourn_date=tourn_date,
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
