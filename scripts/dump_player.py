#!/usr/bin/env python3
"""Emergency NO dump for a given player across Winner/Top3/Top5/Top8.

Fetches live orderbook depth, sums all available NO contracts per market, and
fires fill-or-kill taker orders simultaneously with no ROI or quantity filters.

--player is a case-insensitive substring match against the market's yes_sub_title.
--limit caps the maximum NO price accepted (default 99¢). Orderbook levels priced
above the limit are excluded from both the quantity count and the order price cap,
so only liquidity you're willing to pay for is targeted.

Note: FOK orders fill entirely or not at all. If liquidity changed between the
orderbook snapshot and order submission, orders may be killed. Re-run in that case.

Usage:
    python scripts/dump_player.py --player sarana --date 2026-09-02                   # dry run
    python scripts/dump_player.py --player sarana --date 2026-09-02 --live            # live, up to 99¢ NO
    python scripts/dump_player.py --player sarana --date 2026-09-02 --live --limit 80 # live, skip NO > 80¢
"""

import argparse
import concurrent.futures
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.kalshi_api import KalshiClient, _TT_EVENT_TEMPLATES, _to_kalshi_date


def _build_orders(client: KalshiClient, tourn_date: str, player_filter: str, limit: int) -> list[dict]:
    """Return planned orders, counting only NO liquidity priced at or below limit cents."""
    date_str = _to_kalshi_date(tourn_date)
    yes_bid_floor = (100 - limit) / 100
    orders: list[dict] = []
    for tmpl in _TT_EVENT_TEMPLATES:
        markets = client.get_markets_by_event(tmpl.format(date=date_str))
        for market in markets:
            if player_filter not in market.get("yes_sub_title", "").lower():
                continue
            ticker = market["ticker"]
            ob = client.get_orderbook(ticker)
            yes_bids = ob.get("yes_dollars") or []
            eligible = [(p, q) for p, q in yes_bids if float(p) >= yes_bid_floor]
            qty = sum(int(float(q)) for _, q in eligible)
            if qty:
                orders.append({
                    "ticker":   ticker,
                    "title":    market.get("title", ""),
                    "player":   market.get("yes_sub_title", ""),
                    "qty":      qty,
                    "levels":   len(eligible),
                    "skipped":  len(yes_bids) - len(eligible),
                })
    return orders


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--player", required=True, help="Player name keyword (case-insensitive substring match)")
    parser.add_argument("--date", required=True, help="Tournament date YYYY-MM-DD")
    parser.add_argument("--live", action="store_true", help="Fire orders (default: dry run)")
    parser.add_argument(
        "--limit", type=int, default=99,
        help="Maximum NO price in cents to accept (default: 99). Levels above this are excluded.",
    )
    args = parser.parse_args()
    if not (1 <= args.limit <= 99):
        raise ValueError(f"--limit must be 1–99, got {args.limit}")

    player_filter = args.player.lower()
    client = KalshiClient()

    print(f"Scanning markets for '{args.player}' on {args.date} (NO limit: {args.limit}¢)...")
    orders = _build_orders(client, args.date, player_filter, args.limit)

    label = "" if args.live else "[DRY RUN] "
    print(f"\n{label}NO dump plan — {args.player} — {args.date}:")
    if not orders:
        print(f"  No markets matching '{args.player}' with NO liquidity at or below {args.limit}¢ found.")
        return

    for o in orders:
        skipped_note = f"  (+{o['skipped']} levels skipped > {args.limit}¢)" if o["skipped"] else ""
        print(f"  {o['ticker']:55s}  qty={o['qty']:4d}  ({o['levels']} levels @ ≤{args.limit}¢){skipped_note}")
    print(f"  Total contracts: {sum(o['qty'] for o in orders)}")

    if not args.live:
        print("\n[DRY RUN] No orders placed.")
        return

    def _fire(o: dict) -> bool:
        try:
            client.take_order(
                ticker=o["ticker"], side="no",
                count=o["qty"], price=args.limit,
                dry_run=False,
            )
            print(f"  OK   {o['ticker']}  NO × {o['qty']}")
            return True
        except Exception as e:
            print(f"  FAIL {o['ticker']}  NO × {o['qty']}: {e}")
            return False

    print(f"\nFiring {len(orders)} orders simultaneously...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(orders)) as ex:
        futs = [ex.submit(_fire, o) for o in orders]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    placed = sum(results)
    print(f"\nDone. {placed}/{len(orders)} orders placed.")


if __name__ == "__main__":
    main()
