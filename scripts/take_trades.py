#!/usr/bin/env python3
"""Fire taker orders on high-EV Titled Tuesday markets, with optional continuous loop.

In loop mode the script runs indefinitely:
  - Each iteration fires the single highest-ROI trade per event category
    (winner / top-3 / top-5 / top-8), then waits --interval minutes.
  - If a sweep finds no qualifying trades it backs off for --fallback-wait hours
    before trying again.
  - The loop exits only on KeyboardInterrupt (Ctrl-C).

Usage
-----
    # Dry run, one shot
    python scripts/take_trades.py --date 2026-08-05

    # Live, one shot — buy up to 200 contracts per event category
    python scripts/take_trades.py --date 2026-08-05 --live

    # Live, continuous loop — check every 15 min, back off 1 h when dry
    python scripts/take_trades.py --date 2026-08-05 --live --loop

    # Custom thresholds + loop
    python scripts/take_trades.py --date 2026-08-05 --live --loop \\
        --min-roi 1.3 --min-qty 10 --max-qty 100 --interval 10 --fallback-wait 2
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.kalshi_api import KalshiClient
from src.trading import take_trades


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_once(client: KalshiClient, args: argparse.Namespace) -> int:
    """Run one sweep. Returns edge_count (qualifying trades found, regardless of dry_run)."""
    result = take_trades(
        client,
        tourn_date=args.date,
        min_roi=args.min_roi,
        min_qty=args.min_qty,
        max_qty=args.max_qty,
        best_per_event=True,
        dry_run=not args.live,
    )
    if result["unmatched"]:
        print(f"\nNote: {len(result['unmatched'])} player name(s) could not be matched.")
        print("Add aliases to player_information to resolve them.")
    return result["edge_count"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date", required=True,
        help="Tournament date in YYYY-MM-DD format (e.g. 2026-08-05)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually submit orders. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously until interrupted (Ctrl-C).",
    )
    parser.add_argument(
        "--min-roi", type=float, default=1.5,
        help="Minimum ROI threshold to qualify a trade (default: 1.5)",
    )
    parser.add_argument(
        "--min-qty", type=int, default=25,
        help="Skip levels with fewer available contracts than this (default: 25)",
    )
    parser.add_argument(
        "--max-qty", type=int, default=200,
        help="Cap on contracts purchased per event category (default: 200)",
    )
    parser.add_argument(
        "--interval", type=float, default=15.0,
        help="Minutes to wait between sweeps when trades are found (default: 15)",
    )
    parser.add_argument(
        "--fallback-wait", type=float, default=1.0,
        help="Hours to wait when no qualifying trades are found (default: 1)",
    )
    args = parser.parse_args()

    client = KalshiClient()

    if not args.loop:
        _run_once(client, args)
        return

    print(f"[{_now()}] Starting continuous loop (interval={args.interval}m, fallback={args.fallback_wait}h).")
    print("Press Ctrl-C to stop.\n")
    try:
        while True:
            print(f"[{_now()}] === Sweep ===")
            edge_count = _run_once(client, args)

            if edge_count > 0:
                wait_s = args.interval * 60
                print(f"\n[{_now()}] {edge_count} qualifying trade(s) found. Waiting {args.interval:.0f}m...")
            else:
                wait_s = args.fallback_wait * 3600
                print(f"\n[{_now()}] No qualifying trades. Backing off {args.fallback_wait:.1f}h...")

            time.sleep(wait_s)
    except KeyboardInterrupt:
        print(f"\n[{_now()}] Loop stopped by user.")


if __name__ == "__main__":
    main()
