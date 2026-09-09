#!/usr/bin/env python3
"""Cancel all resting orders for the upcoming Titled Tuesday tournament.

The upcoming Tuesday date is inferred automatically from today's date.
Optionally pass --date to target a specific tournament instead.

Usage
-----
    # Dry run (default — no orders cancelled)
    python scripts/cancel_bids.py

    # Live — actually cancel all resting TT orders
    python scripts/cancel_bids.py --live

    # Target a specific tournament date
    python scripts/cancel_bids.py --date 2026-09-09 --live
"""

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.kalshi_api import KalshiClient, _next_tuesday, _to_kalshi_date


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date", default=None,
        help="Tournament date in YYYY-MM-DD format. Defaults to the soonest upcoming Tuesday.",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually cancel orders. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    target_date = _next_tuesday() if args.date is None else args.date
    date_str = _to_kalshi_date(target_date)
    dry_run = not args.live

    print(f"Target tournament date : {target_date}  ({date_str})")
    print(f"Mode                   : {'DRY RUN' if dry_run else 'LIVE'}\n")

    client = KalshiClient()

    print("Fetching open orders...")
    all_orders = client.get_open_orders()
    print(f"  {len(all_orders)} total resting order(s) across all markets.")

    tt_pat = re.compile(
        rf"(?:KXTITLEDTUESDAY-{date_str}|KXTITLEDTUESTOP-{date_str}T\d+)-"
    )
    tt_orders = [o for o in all_orders if tt_pat.search(o.get("ticker", ""))]

    if not tt_orders:
        print(f"\nNo resting orders found for Titled Tuesday {target_date}.")
        return

    print(f"  {len(tt_orders)} order(s) matched Titled Tuesday {target_date}:\n")
    for o in tt_orders:
        side  = o.get("side", "?")
        price = o.get("price", "?")
        count = o.get("remaining_count", o.get("count", "?"))
        print(f"  {'[DRY RUN] ' if dry_run else ''}Cancelling {o['ticker']}  side={side}  price={price}  qty={count}  id={o['order_id']}")
        if not dry_run:
            client.cancel_order(o["order_id"])

    print(f"\n{'Would cancel' if dry_run else 'Cancelled'} {len(tt_orders)} order(s).")
    if dry_run:
        print("Pass --live to execute.")


if __name__ == "__main__":
    main()
