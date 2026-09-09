"""Kalshi bid-placement bot for Titled Tuesday markets."""
from __future__ import annotations

import concurrent.futures
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .config import DB_PATH, TOURN_DATE
from .data import get_username_mappings
from .kalshi_api import KalshiClient, _TT_EVENT_TEMPLATES, _to_kalshi_date

_TEMPLATE_N: dict[str, int] = {
    "KXTITLEDTUESDAY-{date}":   1,
    "KXTITLEDTUESTOP-{date}T3": 3,
    "KXTITLEDTUESTOP-{date}T5": 5,
    "KXTITLEDTUESTOP-{date}T8": 8,
}

_N_TO_COL: dict[int, str] = {
    1: "P_top1_given_play",
    3: "P_top3_given_play",
    5: "P_top5_given_play",
    8: "P_top8_given_play",
}

_ET = ZoneInfo("America/New_York")


def _expiry_ts(tourn_date: str) -> int:
    naive = datetime.strptime(tourn_date + " 09:00:00", "%Y-%m-%d %H:%M:%S")
    return int(naive.replace(tzinfo=_ET).timestamp())  # seconds


def _bid_cents(fair: float, markup: float = 1.5, max_discount: float = 10) -> int | None:
    if fair <= 0:
        return None
    c = round(max(fair / markup, fair - max_discount/100) * 100)
    return c if 1 <= c <= 99 else None


def _best_ask(ob: dict, side: str) -> float | None:
    # YES ask = lowest NO resting bid price subtracted from 1; vice versa
    bids = ob.get("no_dollars" if side == "yes" else "yes_dollars") or []
    if not bids:
        return None
    return 1.0 - max(float(p) for p, _ in bids)


def _apply_pullback(bid_c: int, ask: float | None) -> int:
    if ask is None:
        return bid_c
    ask_c = round(ask * 100)
    return max(ask_c - 1, 1) if ask_c <= bid_c else bid_c


def _load_predictions() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM latest_model_predictions", conn)
    conn.close()
    return df.set_index("username")


def place_bids(
    client: KalshiClient,
    tourn_date: str = TOURN_DATE,
    count: int = 200,
    markup: float = 1.5,
    max_discount: float = 10,
    side_filter: str | None = None,
    sleep_read: float = 0.05,
    sleep_write: float = 0.10,
    dry_run: bool = True,
) -> dict:
    """Place resting bids for all Titled Tuesday markets on tourn_date.

    markup: divisor applied to fair price (bid = fair / markup). Default 1.5 → bid = 2/3 fair.
    max_discount: maximum cents-off cap so bid >= fair - max_discount.
    side_filter: if "yes" or "no", only place bids on that side.

    Returns a summary dict: {placed, cancelled, skipped, unmatched}.
    """
    expiry = _expiry_ts(tourn_date)
    date_str = _to_kalshi_date(tourn_date)
    _, PLAYER_TO_USERNAME = get_username_mappings()

    # ── Phase 1: Reads ────────────────────────────────────────────────────────
    print("Loading model predictions from DB...")
    preds = _load_predictions()

    # Index open orders by (ticker, side) -> list of orders
    open_by_key: dict[tuple[str, str], list[dict]] = {}
    if not dry_run:
        print("Fetching open orders...")
        open_orders = client.get_open_orders()
        time.sleep(sleep_read)
        for o in open_orders:
            # Skip user's resting YES sell orders (take-profit sells)
            # Our NO bids appear as action="sell" + outcome_side="no" — keep those
            if o.get("action") == "sell" and o.get("outcome_side") == "yes":
                continue
            side = o.get("outcome_side", "yes")  # "yes" or "no"
            key = (o["ticker"], side)
            open_by_key.setdefault(key, []).append(o)
    else:
        print("Skipping open orders fetch (dry run — cancel-and-replace not evaluated).")

    print("Fetching markets and orderbooks...")
    # market_data: list of (ticker, title, yes_sub_title, n_value, orderbook)
    market_data: list[tuple[str, str, str, int, dict]] = []
    for tmpl, n in _TEMPLATE_N.items():
        event_ticker = tmpl.format(date=date_str)
        markets = client.get_markets_by_event(event_ticker)
        time.sleep(sleep_read)
        for m in markets:
            ticker = m["ticker"]
            ob = client.get_orderbook(ticker)
            time.sleep(sleep_read)
            market_data.append((ticker, m.get("title", ""), m.get("yes_sub_title", ""), n, ob))

    # ── Phase 2: Plan ─────────────────────────────────────────────────────────
    # plan: list of actions per (ticker, side)
    # action = {"ticker", "side", "bid_c", "cancel": [order_ids], "skip_reason": str|None}
    plan: list[dict] = []
    unmatched: list[str] = []

    for ticker, title, subtitle, n, ob in market_data:
        col = _N_TO_COL[n]

        # Resolve player name to username
        username = PLAYER_TO_USERNAME.get(subtitle)
        if username is None:
            unmatched.append(subtitle)
            continue
        if username not in preds.index:
            unmatched.append(subtitle)
            continue

        row = preds.loc[username]
        p_participate = float(row["p_participate"])
        p_topn = float(row[col])

        fair_yes = p_participate * p_topn
        fair_no  = 1.0 - fair_yes

        sides = [("yes", fair_yes), ("no", fair_no)]
        if side_filter:
            sides = [(s, f) for s, f in sides if s == side_filter]
        for side, fair in sides:
            key = (ticker, side)
            bid_c = _bid_cents(fair, markup, max_discount)
            if bid_c is None:
                plan.append({
                    "ticker": ticker, "side": side,
                    "bid_c": None, "cancel": [],
                    "skip_reason": f"fair={fair:.4f} maps to invalid bid",
                })
                continue

            ask = _best_ask(ob, side)
            bid_c = _apply_pullback(bid_c, ask)

            existing = open_by_key.get(key, [])
            if existing:
                price_field = "yes_price_dollars" if side == "yes" else "no_price_dollars"
                min_existing = min(
                    round(float(o[price_field]) * 100) for o in existing
                )
                if bid_c <= min_existing:
                    # New price is lower or equal → cancel all, place fresh at correct count
                    plan.append({
                        "ticker": ticker, "side": side,
                        "bid_c": bid_c,
                        "cancel": [o["order_id"] for o in existing],
                        "skip_reason": None,
                    })
                else:
                    # Existing order is more conservative (lower price) → leave it but place a new order.
                    plan.append({
                        "ticker": ticker, "side": side,
                        "bid_c": bid_c, "cancel": [],
                        "skip_reason": None,
                    })
            else:
                plan.append({
                    "ticker": ticker, "side": side,
                    "bid_c": bid_c, "cancel": [],
                    "skip_reason": None,
                })

    # ── Phase 3: Summary ──────────────────────────────────────────────────────
    to_place    = [p for p in plan if p["skip_reason"] is None and p["bid_c"] is not None]
    to_skip     = [p for p in plan if p["skip_reason"] is not None]
    to_cancel   = [oid for p in plan for oid in p["cancel"]]

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Bid placement plan for {tourn_date}:")
    print(f"  Markets scanned : {len(market_data)}")
    print(f"  Unmatched names : {len(unmatched)}")
    print(f"  Bids to place   : {len(to_place)}")
    print(f"  Orders to cancel: {len(to_cancel)}")
    print(f"  Skipped         : {len(to_skip)}")

    if unmatched:
        print(f"\n  Unmatched player names:")
        for name in unmatched:
            print(f"    - {name!r}")

    if to_skip:
        print(f"\n  Skipped bids:")
        for p in to_skip:
            print(f"    {p['ticker']} {p['side'].upper():3s} — {p['skip_reason']}")

    print(f"\n  Planned bids:")
    for p in to_place:
        cancel_note = f" (cancelling {len(p['cancel'])} existing)" if p["cancel"] else ""
        print(f"    {p['ticker']} {p['side'].upper():3s} @ {p['bid_c']}¢ × {count}{cancel_note}")

    if dry_run:
        print("\n[DRY RUN] No orders placed or cancelled.")
        return {
            "placed": 0, "cancelled": 0,
            "skipped": len(to_skip), "unmatched": unmatched,
        }

    # ── Phase 4: Execute ──────────────────────────────────────────────────────
    cancelled = 0
    placed    = 0

    # Cancels first
    for p in to_place:
        for oid in p["cancel"]:
            try:
                client.cancel_order(oid)
                cancelled += 1
                print(f"  Cancelled {oid}")
            except Exception as e:
                print(f"  WARNING: could not cancel {oid}: {e}")
            time.sleep(sleep_write)

    # Then placements
    for p in to_place:
        try:
            client.place_order(
                ticker=p["ticker"],
                side=p["side"],
                count=count,
                price=p["bid_c"],
                expiration_ts=expiry,
                dry_run=False,
            )
            placed += 1
            print(f"  Placed {p['ticker']} {p['side'].upper()} @ {p['bid_c']}¢")
        except Exception as e:
            print(f"  WARNING: could not place {p['ticker']} {p['side'].upper()}: {e}")
        time.sleep(sleep_write)

    print(f"\nDone. Placed {placed}, cancelled {cancelled}, skipped {len(to_skip)}.")
    return {
        "placed": placed, "cancelled": cancelled,
        "skipped": len(to_skip), "unmatched": unmatched,
    }


def _kalshi_order_price(ask: float) -> float:
    return ask + 0.07 * ask * (1.0 - ask)


def _parse_n(title: str) -> int:
    if re.search('win the Titled Tuesday weekly chess competition, originally scheduled for', title or '') is not None:
            return 1
    m = re.search(r'\b(Top\s+)?(\d+)\b', title or '')
    if not m:
        return 1
    val = int(m.group(2))
    return val if val <= 20 else 1


def take_trades(
    client: KalshiClient,
    tourn_date: str,
    min_roi: float = 1.5,
    min_qty: int = 10,
    max_qty: int = 200,
    sleep_read: float = 0.05,
    best_per_event: bool = True,
    dry_run: bool = True,
    budget_remaining: float | None = None,
) -> dict:
    """Fetch live asks, compute EV, and fire qualifying taker orders.

    best_per_event: if True (default), fires only the single highest-ROI trade per
        N-category (winner / top3 / top5 / top8). This causes the market maker to
        pull back all remaining quotes in that category, so further trades in the
        same event are expected to be stale; subsequent loop iterations capture them.
        If False, fires every qualifying level per (ticker, side) simultaneously.

    min_qty: levels with fewer available contracts are skipped as too thin.
    max_qty: cap on contracts purchased per level; actual count = min(available, max_qty).
    budget_remaining: if set, the batch is cancelled and budget_exceeded=True is returned
        if proposed exposure would exceed this value.

    Returns a summary dict: {placed, failed, skipped, unmatched, edge_count, exposure, budget_exceeded}.
    edge_count reflects qualifying trades found regardless of dry_run.
    exposure is the proposed dollar cost of the batch (ask_price × qty summed across orders).
    """
    _, PLAYER_TO_USERNAME = get_username_mappings()

    print("Loading model predictions from DB...")
    preds = _load_predictions()
    for n in [1, 3, 5, 8, 10]:
        col_given = f"P_top{n}_given_play"
        if col_given in preds.columns:
            preds[f"P_top{n}"] = preds["p_participate"] * preds[col_given]

    print(f"Fetching live asks for {tourn_date}...")
    all_asks = client.get_titled_tuesday_asks(tourn_date, sleep=sleep_read)
    print(f"Fetched {len(all_asks)} orderbook levels.")

    if not all_asks:
        print("No asks found.")
        return {"placed": 0, "failed": 0, "skipped": 0, "unmatched": [], "edge_count": 0}

    df = pd.DataFrame(all_asks)
    df["n"] = df["Market Title"].apply(_parse_n)
    df["username"] = df["Player Name"].map(lambda name: PLAYER_TO_USERNAME.get(name))

    unmatched = df[df["username"].isna()]["Player Name"].unique().tolist()
    df = df[df["username"].notna()].copy()

    evs = []
    for _, row in df.iterrows():
        col = f"P_top{int(row['n'])}"
        if row["username"] not in preds.index or col not in preds.columns:
            evs.append(None)
            continue
        p_yes = float(preds.loc[row["username"], col])
        evs.append(p_yes if row["Side"] == "YES" else 1.0 - p_yes)

    df["EV"] = evs
    df = df[df["EV"].notna()].copy()
    df["Order Price"] = df["Ask Price ($)"].apply(_kalshi_order_price)
    df["ROI"] = df["EV"] / df["Order Price"]

    has_volume   = df["Quantity Available"] >= min_qty
    has_edge     = df["ROI"] > min_roi
    thin_skipped = int((has_edge & ~has_volume).sum())

    qualifying = df[has_edge & has_volume]
    if best_per_event:
        # One trade per N-category: highest ROI per event group.
        edge = (
            qualifying
            .sort_values("ROI", ascending=False)
            .drop_duplicates(subset=["n"])
            .copy()
        )
    else:
        # All qualifying levels: best (lowest) ask per (ticker, side).
        edge = (
            qualifying
            .sort_values("Ask Price ($)")
            .drop_duplicates(subset=["Market Ticker", "Side"])
            .copy()
        )
    edge["order_qty"] = edge["Quantity Available"].clip(upper=max_qty).astype(int)
    proposed_exposure = float((edge["Ask Price ($)"] * edge["order_qty"]).sum())

    mode_label = "best-per-event" if best_per_event else "all-qualifying"
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Taker trade plan for {tourn_date} (ROI > {min_roi}, mode={mode_label}):")
    print(f"  Levels scanned  : {len(df)}")
    print(f"  Edge trades     : {len(edge)}")
    print(f"  Thin (skipped)  : {thin_skipped}  (ROI OK but qty < {min_qty})")
    print(f"  Unmatched names : {len(unmatched)}")

    if unmatched:
        print(f"\n  Unmatched:")
        for name in unmatched:
            print(f"    - {name!r}")

    if edge.empty:
        print("  No trades to place.")
        return {"placed": 0, "failed": 0, "skipped": len(df), "unmatched": unmatched, "edge_count": 0, "exposure": 0.0, "budget_exceeded": False}

    print(f"\n  Planned taker orders (proposed exposure: ${proposed_exposure:.2f}):")
    for _, row in edge.sort_values("ROI", ascending=False).iterrows():
        ask_c = round(row["Ask Price ($)"] * 100)
        print(
            f"    {row['Market Ticker']} {row['Side']:3s} @ {ask_c}¢"
            f"  ROI={row['ROI']:.3f}  EV={row['EV']:.3f}"
            f"  qty={row['order_qty']} (avail={int(row['Quantity Available'])})"
        )

    if budget_remaining is not None and proposed_exposure > budget_remaining:
        print(f"\n  Budget check: proposed ${proposed_exposure:.2f} exceeds remaining ${budget_remaining:.2f}. Cancelling batch.")
        return {"placed": 0, "failed": 0, "skipped": len(df) - len(edge), "unmatched": unmatched, "edge_count": len(edge), "exposure": 0.0, "budget_exceeded": True}

    if dry_run:
        print("\n[DRY RUN] No orders placed.")
        return {"placed": 0, "failed": 0, "skipped": len(df) - len(edge), "unmatched": unmatched, "edge_count": len(edge), "exposure": proposed_exposure, "budget_exceeded": False}

    # Fire all simultaneously
    def _take_one(row: pd.Series) -> tuple[bool, str, str, int]:
        ticker = row["Market Ticker"]
        side   = row["Side"].lower()
        ask_c  = round(row["Ask Price ($)"] * 100)
        qty    = int(row["order_qty"])
        try:
            client.take_order(ticker=ticker, side=side, count=qty, price=ask_c, dry_run=False)
            print(f"  Placed {ticker} {side.upper()} @ {ask_c}¢ × {qty}")
            return (True, ticker, side, ask_c)
        except Exception as e:
            print(f"  FAILED {ticker} {side.upper()} @ {ask_c}¢ × {qty}: {e}")
            return (False, ticker, side, ask_c)

    rows = [row for _, row in edge.iterrows()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rows)) as executor:
        futures = [executor.submit(_take_one, row) for row in rows]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    placed = sum(1 for ok, *_ in results if ok)
    failed = len(results) - placed
    print(f"\nDone. Placed {placed}, failed {failed}, skipped {len(df) - len(edge)}.")
    return {"placed": placed, "failed": failed, "skipped": len(df) - len(edge), "unmatched": unmatched, "edge_count": len(edge), "exposure": proposed_exposure, "budget_exceeded": False}
