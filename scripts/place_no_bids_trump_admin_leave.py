#!/usr/bin/env python3
"""Place resting NO buy orders on every open market in KXTRUMPADMINLEAVE.

For each open market in the event:
  - Fetches the current orderbook
  - Reads the best NO bid (highest price in no_dollars)
  - Places a resting limit order: buy CONTRACTS NO contracts at that price

Usage
-----
    # Dry run (default — prints orders but does not submit)
    python scripts/place_no_bids_trump_admin_leave.py

    # Live — actually submit orders
    python scripts/place_no_bids_trump_admin_leave.py --execute
"""

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.kalshi_api import KalshiClient

EVENT_TICKER = "KXTRUMPADMINLEAVE-26DEC31"
CONTRACTS    = 50


def main(dry_run: bool) -> None:
    client = KalshiClient()

    print(f"Fetching open markets for event: {EVENT_TICKER}")
    markets = client.get_markets_by_event(EVENT_TICKER)
    if not markets:
        print("No open markets found.")
        return
    print(f"Found {len(markets)} open market(s).\n")

    placed, skipped = 0, 0
    for market in markets:
        ticker = market["ticker"]
        title  = market.get("title", ticker)

        try:
            ob = client.get_orderbook(ticker)
        except Exception as e:
            print(f"[{ticker}] ERROR fetching orderbook: {e}")
            skipped += 1
            continue

        no_bids = ob.get("no_dollars") or []
        if not no_bids:
            print(f"[{ticker}] No resting NO bids — skipping.  ({title})")
            skipped += 1
            continue

        # no_dollars is sorted descending (best bid first); use max() defensively
        best_no_price = max(float(p) for p, _ in no_bids)
        price_cents   = round(best_no_price * 100)

        if not (1 <= price_cents <= 99):
            print(f"[{ticker}] NO bid price {price_cents}¢ out of range — skipping.")
            skipped += 1
            continue

        print(f"[{ticker}]  {title}")
        print(f"  NO bid: {price_cents}¢  → resting buy {CONTRACTS} NO @ {price_cents}¢")

        try:
            client.place_order(ticker, "no", CONTRACTS, price_cents, dry_run=dry_run)
            placed += 1
        except Exception as e:
            print(f"  ERROR placing order: {e}")
            skipped += 1

        time.sleep(0.1)

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n[{mode}] Done — {placed} order(s) placed, {skipped} market(s) skipped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Place NO bids on KXTRUMPADMINLEAVE markets.")
    parser.add_argument("--execute", action="store_true", help="Submit live orders (default: dry run)")
    args = parser.parse_args()
    main(dry_run=not args.execute)
