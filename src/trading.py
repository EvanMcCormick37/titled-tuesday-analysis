"""Kalshi bid-placement and taker strategies for Titled Tuesday markets.

Each strategy has a read-only `plan_*` function returning a `Plan`, which
`trade_plan.execute_plan` sends to Kalshi. The job worker uses those directly.
The original `place_bids` / `place_fade_no_bids` / `take_trades` /
`cancel_tt_bids` functions remain as print-based wrappers for the CLI scripts,
notebook, and Telegram bot.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from kalshi_core import KalshiClient, _to_kalshi_date, bid_cents, best_ask, apply_pullback, kalshi_order_price
from .data import get_username_mappings
from .db import get_engine
from .kalshi_tt import _TT_EVENT_TEMPLATES, fetch_orderbooks, get_tt_asks
from .trade_plan import CancelIntent, Emit, OrderIntent, Plan, execute_plan, format_plan, noop_emit, print_emit

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
    return int(naive.replace(tzinfo=_ET).timestamp())


# TT tournament starts at 11:00 AM ET.
_TOURN_START_HOUR, _TOURN_START_MIN = 11, 0


def _expiry_ts_after_start(tourn_date: str, minutes_after_start: int) -> int:
    """Return Unix TS for `tourn_date` at (11:00 ET + minutes_after_start)."""
    naive = datetime.strptime(tourn_date, "%Y-%m-%d")
    start = naive.replace(hour=_TOURN_START_HOUR, minute=_TOURN_START_MIN,
                          tzinfo=_ET)
    return int((start + timedelta(minutes=minutes_after_start)).timestamp())


def _parse_kalshi_expiration(order: dict) -> int | None:
    """Parse an open-order's `expiration_time` ISO string to a Unix TS.

    Kalshi returns strings like '2026-10-06T15:00:00Z'. Returns None if the
    field is missing or unparseable.
    """
    raw = order.get("expiration_time")
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def _load_predictions() -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM latest_model_predictions", get_engine())
    return df.set_index("username")


def _parse_n(title: str) -> int:
    if re.search("Winner", title or "") is not None:
        return 1
    m = re.search(r"\b(Top\s+)(\d+)\b", title or "")
    if not m:
        return 1
    val = int(m.group(2))
    return val if val <= 20 else 1


def _order_cents(order: dict) -> int | None:
    field = "no_price_dollars" if order.get("outcome_side") == "no" else "yes_price_dollars"
    try:
        return round(float(order[field]) * 100)
    except (KeyError, TypeError, ValueError):
        return None


def _cancel_intent(order: dict) -> CancelIntent:
    return CancelIntent(
        order_id=order["order_id"],
        ticker=order.get("ticker", ""),
        side=order.get("outcome_side") or order.get("side") or "",
        price_c=_order_cents(order),
        qty=order.get("remaining_count", order.get("count")),
    )


def _fetch_tt_markets(
    client: KalshiClient, date_str: str, sleep_read: float,
) -> list[tuple[str, str, str, int]]:
    """Return (ticker, title, yes_sub_title, n) for every market on the date."""
    rows: list[tuple[str, str, str, int]] = []
    for tmpl, n in _TEMPLATE_N.items():
        for m in client.get_markets_by_event(tmpl.format(date=date_str)):
            rows.append((m["ticker"], m.get("title", ""), m.get("yes_sub_title", ""), n))
        if sleep_read:
            time.sleep(sleep_read)
    return rows


# ── Plans (read-only) ────────────────────────────────────────────────────────

def plan_place_bids(
    client: KalshiClient,
    tourn_date: str | None = None,
    count: int = 200,
    markup: float = 1.5,
    max_discount: float = 10,
    side_filter: str | None = None,
    sleep_read: float = 0.05,
    emit: Emit = noop_emit,
) -> Plan:
    """Plan resting bids for all Titled Tuesday markets on tourn_date.

    markup: divisor applied to fair price (bid = fair / markup).
    max_discount: maximum cents-off cap so bid >= fair - max_discount.
    side_filter: if "yes" or "no", only place bids on that side.

    Always reads open orders, so a preview shows the real cancel-and-replace set.
    """
    if tourn_date is None:
        from .pipeline import next_tourn_date
        tourn_date = next_tourn_date()
    expiry = _expiry_ts(tourn_date)
    date_str = _to_kalshi_date(tourn_date)
    _, player_to_username = get_username_mappings()

    emit("phase", "Loading model predictions")
    preds = _load_predictions()

    emit("phase", "Fetching open orders")
    open_by_key: dict[tuple[str, str], list[dict]] = {}
    for o in client.get_open_orders():
        if o.get("action") == "sell" and o.get("outcome_side") == "yes":
            continue
        open_by_key.setdefault((o["ticker"], o.get("outcome_side", "yes")), []).append(o)

    emit("phase", "Fetching markets and orderbooks")
    markets = _fetch_tt_markets(client, date_str, sleep_read)
    books = fetch_orderbooks(client, [m[0] for m in markets])

    plan = Plan(strategy="place_bids", tourn_date=tourn_date)
    plan.stats["markets_scanned"] = len(markets)
    cancel_ids_seen: set[str] = set()

    for ticker, _title, subtitle, n in markets:
        username = player_to_username.get(subtitle)
        if username is None or username not in preds.index:
            plan.unmatched.append(subtitle)
            continue
        ob = books.get(ticker)
        if ob is None:
            plan.skipped.append({"ticker": ticker, "reason": "orderbook unavailable"})
            continue

        row = preds.loc[username]
        fair_yes = float(row["p_participate"]) * float(row[_N_TO_COL[n]])
        sides = [("yes", fair_yes), ("no", 1.0 - fair_yes)]
        if side_filter:
            sides = [(s, f) for s, f in sides if s == side_filter]

        for side, fair in sides:
            bid_c = bid_cents(fair, markup, max_discount)
            if bid_c is None:
                plan.skipped.append({
                    "ticker": ticker, "side": side,
                    "reason": f"{side.upper()} fair={fair:.4f} maps to invalid bid",
                })
                continue
            bid_c = apply_pullback(bid_c, best_ask(ob, side))

            existing = open_by_key.get((ticker, side), [])
            to_cancel: list[dict] = []
            if existing:
                price_field = "yes_price_dollars" if side == "yes" else "no_price_dollars"
                min_existing = min(round(float(o[price_field]) * 100) for o in existing)
                if bid_c <= min_existing:
                    to_cancel = existing

            for o in to_cancel:
                if o["order_id"] not in cancel_ids_seen:
                    cancel_ids_seen.add(o["order_id"])
                    plan.cancels.append(_cancel_intent(o))
            plan.orders.append(OrderIntent(
                kind="place", ticker=ticker, side=side, count=count, price_c=bid_c,
                expiration_ts=expiry, label=subtitle,
                meta={"n": n, "fair": round(fair, 4), "replaces": len(to_cancel)},
            ))
    return plan


def plan_fade_no_bids(
    client: KalshiClient,
    tourn_date: str,
    discount_cents: int = 15,
    count: int = 200,
    minutes_after_start: int = 10,
    min_bid_cents: int = 1,
    max_bid_cents: int = 99,
    sleep_read: float = 0.05,
    emit: Emit = noop_emit,
) -> Plan:
    """Plan resting NO bids at (fair_NO_given_plays − discount) cents, expiring
    `minutes_after_start` minutes after tournament start (11:00 ET).

    Strategy — fade retail overpayment of YES when a low-attendance star shows
    up on the roster. Prices NO using `P_top{N}_given_play`, ignoring
    p_participate, because the fade assumes the player has already shown up.

    Only NO bids expiring at or after tournament start are managed, so the
    9 AM place_bids NO orders are never cancelled by this strategy:
        - No existing post-start NO bid              → place at target
        - Existing post-start NO bid at target       → leave it (idempotent)
        - Existing post-start NO bid at other price  → cancel + replace
    """
    expiry = _expiry_ts_after_start(tourn_date, minutes_after_start)
    expiry_iso = datetime.fromtimestamp(expiry, _ET).isoformat()
    tourn_start_ts = _expiry_ts_after_start(tourn_date, 0)
    date_str = _to_kalshi_date(tourn_date)
    _, player_to_username = get_username_mappings()

    emit("phase", "Loading model predictions")
    preds = _load_predictions()

    emit("phase", "Fetching open NO orders")
    open_by_ticker: dict[str, list[dict]] = {}
    for o in client.get_open_orders():
        if o.get("outcome_side") != "no" or o.get("action") == "sell":
            continue
        open_by_ticker.setdefault(o["ticker"], []).append(o)

    emit("phase", "Fetching markets")
    markets = _fetch_tt_markets(client, date_str, sleep_read)

    plan = Plan(strategy="fade_no_bids", tourn_date=tourn_date,
                stats={"markets_scanned": len(markets), "expires": expiry_iso},
                extra={"expiry_iso": expiry_iso})

    for ticker, _title, subtitle, n in markets:
        username = player_to_username.get(subtitle)
        if username is None or username not in preds.index:
            plan.unmatched.append(subtitle)
            continue

        p_cond = float(preds.loc[username, _N_TO_COL[n]])
        fair_no_cents = round((1.0 - p_cond) * 100)
        target_bid = fair_no_cents - discount_cents

        if target_bid < min_bid_cents or target_bid > max_bid_cents:
            plan.skipped.append({
                "ticker": ticker, "subtitle": subtitle,
                "reason": f"target bid {target_bid}¢ outside [{min_bid_cents},{max_bid_cents}] "
                          f"(P(top-{n}|plays)={p_cond:.3f})",
            })
            continue

        existing_post_start = [
            o for o in open_by_ticker.get(ticker, [])
            if (ts := _parse_kalshi_expiration(o)) is not None and ts >= tourn_start_ts
        ]
        if any(round(float(o.get("no_price_dollars", 0)) * 100) == target_bid
               for o in existing_post_start):
            plan.skipped.append({
                "ticker": ticker, "subtitle": subtitle,
                "reason": f"NO bid already resting at {target_bid}¢ with post-start expiry",
            })
            continue

        plan.cancels.extend(_cancel_intent(o) for o in existing_post_start)
        plan.orders.append(OrderIntent(
            kind="place", ticker=ticker, side="no", count=count, price_c=target_bid,
            expiration_ts=expiry, label=subtitle,
            meta={"n": n, "p_cond": p_cond, "fair_no_cents": fair_no_cents,
                  "cancel": [o["order_id"] for o in existing_post_start]},
        ))
    plan.orders.sort(key=lambda o: o.price_c)
    return plan


def plan_take_trades(
    client: KalshiClient,
    tourn_date: str,
    min_roi: float = 1.5,
    min_qty: int = 10,
    max_qty: int = 200,
    best_per_event: bool = True,
    sleep_read: float = 0.05,
    emit: Emit = noop_emit,
) -> Plan:
    """Plan fill-or-kill taker orders on levels whose ROI clears `min_roi`.

    best_per_event: if True, only the single highest-ROI trade per N-category.
        If False, every qualifying level per (ticker, side).
    min_qty: levels with fewer available contracts are skipped as iceberg decoys.
    max_qty: cap on contracts purchased per level.
    """
    emit("phase", "Fetching live asks")
    df, unmatched = compute_edges(client, tourn_date, sleep_read=sleep_read)
    plan = Plan(strategy="take_trades", tourn_date=tourn_date, unmatched=unmatched,
                stats={"levels_scanned": len(df), "edge_count": 0, "thin_skipped": 0})
    if df.empty:
        return plan

    has_volume = df["Quantity Available"] >= min_qty
    has_edge   = df["ROI"] > min_roi
    plan.stats["thin_skipped"] = int((has_edge & ~has_volume).sum())

    qualifying = df[has_edge & has_volume]
    if best_per_event:
        edge = qualifying.sort_values("ROI", ascending=False).drop_duplicates(subset=["n"]).copy()
    else:
        edge = qualifying.sort_values("Ask Price ($)").drop_duplicates(
            subset=["Market Ticker", "Side"]).copy()

    edge["order_qty"] = edge["Quantity Available"].clip(upper=max_qty).astype(int)
    plan.stats["edge_count"] = len(edge)
    plan.stats["mode"] = "best-per-event" if best_per_event else "all-qualifying"

    for _, row in edge.sort_values("ROI", ascending=False).iterrows():
        plan.orders.append(OrderIntent(
            kind="take", ticker=row["Market Ticker"], side=row["Side"].lower(),
            count=int(row["order_qty"]), price_c=round(row["Ask Price ($)"] * 100),
            label=row["Player Name"],
            meta={"n": int(row["n"]), "roi": round(float(row["ROI"]), 3),
                  "ev": round(float(row["EV"]), 3),
                  "available": int(row["Quantity Available"])},
        ))
    return plan


def plan_cancel_bids(
    client: KalshiClient,
    tourn_date: str,
    emit: Emit = noop_emit,
) -> Plan:
    """Plan cancelling every resting TT order for `tourn_date`."""
    date_str = _to_kalshi_date(tourn_date)
    tt_pat = re.compile(rf"(?:KXTITLEDTUESDAY-{date_str}|KXTITLEDTUESTOP-{date_str}T\d+)-")

    emit("phase", "Fetching open orders")
    all_orders = client.get_open_orders()
    tt_orders = [o for o in all_orders if tt_pat.search(o.get("ticker", ""))]
    return Plan(
        strategy="cancel_bids", tourn_date=tourn_date,
        cancels=[_cancel_intent(o) for o in tt_orders],
        stats={"open_orders": len(all_orders), "tt_orders": len(tt_orders)},
        extra={"orders": tt_orders},
    )


# ── Shared edge calculation ──────────────────────────────────────────────────

def compute_edges(
    client: KalshiClient,
    tourn_date: str,
    sleep_read: float = 0.05,
    verbose: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch live asks for `tourn_date` and enrich with model EV/ROI/Expected Return.

    Returns (df, unmatched_player_names).

    DataFrame columns (one row per ask level, kept rows have matched usernames
    and computable EV):
        Market Ticker, Market Title, Player Name, Side, Ask Price ($),
        Quantity Available, n, username, EV, Order Price, Total Cost,
        Expected Return, ROI

    Shared between `take_trades` (filter → fire) and the Edges view (filter → display).
    """
    _, PLAYER_TO_USERNAME = get_username_mappings()

    if verbose:
        print("Loading model predictions from DB...")
    preds = _load_predictions()
    for n in [1, 3, 5, 8, 10]:
        col_given = f"P_top{n}_given_play"
        if col_given in preds.columns:
            preds[f"P_top{n}"] = preds["p_participate"] * preds[col_given]

    if verbose:
        print(f"Fetching live asks for {tourn_date}...")
    all_asks = get_tt_asks(client, tourn_date, sleep=sleep_read)
    if verbose:
        print(f"Fetched {len(all_asks)} orderbook levels.")

    if not all_asks:
        return pd.DataFrame(), []

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
    df["Order Price"]     = df["Ask Price ($)"].apply(kalshi_order_price)
    df["Total Cost"]      = df["Quantity Available"] * df["Order Price"]
    df["Expected Return"] = (df["EV"] - df["Order Price"]) * df["Quantity Available"].astype(int)
    df["ROI"]             = df["EV"] / df["Order Price"]
    return df, unmatched


# ── Print-based wrappers (CLI scripts, notebook, Telegram bot) ───────────────

def place_bids(
    client: KalshiClient,
    tourn_date: str | None = None,
    count: int = 200,
    markup: float = 1.5,
    max_discount: float = 10,
    side_filter: str | None = None,
    sleep_read: float = 0.05,
    sleep_write: float = 0.10,
    dry_run: bool = True,
) -> dict:
    """Plan and (unless dry_run) place resting bids. Returns {placed, cancelled, skipped, unmatched}."""
    plan = plan_place_bids(client, tourn_date, count, markup, max_discount, side_filter,
                           sleep_read, emit=print_emit)
    print(format_plan(plan, dry_run))
    if dry_run:
        return {"placed": 0, "cancelled": 0, "skipped": len(plan.skipped), "unmatched": plan.unmatched}
    result = execute_plan(client, plan, print_emit, sleep_write=sleep_write)
    print(f"\nDone. Placed {result.placed}, cancelled {result.cancelled}, skipped {len(plan.skipped)}.")
    return {"placed": result.placed, "cancelled": result.cancelled,
            "skipped": len(plan.skipped), "unmatched": plan.unmatched}


def cancel_tt_bids(
    client: KalshiClient,
    tourn_date: str,
    dry_run: bool = True,
) -> dict:
    """Cancel all resting TT bids. Returns {matched, cancelled, orders}."""
    plan = plan_cancel_bids(client, tourn_date, emit=print_emit)
    print(format_plan(plan, dry_run))
    result = execute_plan(client, plan, print_emit, sleep_write=0) if not dry_run else None
    return {"matched": len(plan.cancels),
            "cancelled": result.cancelled if result else 0,
            "orders": plan.extra["orders"]}


def place_fade_no_bids(
    client: KalshiClient,
    tourn_date: str,
    discount_cents: int = 15,
    count: int = 200,
    minutes_after_start: int = 10,
    min_bid_cents: int = 1,
    max_bid_cents: int = 99,
    sleep_read: float = 0.05,
    sleep_write: float = 0.10,
    dry_run: bool = True,
) -> dict:
    """Plan and (unless dry_run) place fade NO bids.

    Returns {placed, cancelled, skipped, unmatched, expiry_iso, plan}.
    """
    plan = plan_fade_no_bids(client, tourn_date, discount_cents, count, minutes_after_start,
                             min_bid_cents, max_bid_cents, sleep_read, emit=print_emit)
    print(format_plan(plan, dry_run))
    rows = [{"ticker": o.ticker, "subtitle": o.label, "bid_c": o.price_c, **o.meta}
            for o in plan.orders]
    base = {"skipped": len(plan.skipped), "unmatched": plan.unmatched,
            "expiry_iso": plan.extra["expiry_iso"], "plan": rows}
    if dry_run:
        return {"placed": 0, "cancelled": 0, **base}
    result = execute_plan(client, plan, print_emit, sleep_write=sleep_write)
    print(f"\nDone. Placed {result.placed}, cancelled {result.cancelled}, skipped {len(plan.skipped)}.")
    return {"placed": result.placed, "cancelled": result.cancelled, **base}


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
    """Plan and (unless dry_run) fire taker orders.

    budget_remaining: if set, cancels the batch if proposed exposure exceeds this.

    Returns {placed, failed, skipped, unmatched, edge_count, exposure, budget_exceeded}.
    On a live run `exposure` counts only orders that actually filled.
    """
    plan = plan_take_trades(client, tourn_date, min_roi, min_qty, max_qty, best_per_event,
                            sleep_read, emit=print_emit)
    print(format_plan(plan, dry_run))
    base = {"unmatched": plan.unmatched, "edge_count": plan.stats["edge_count"],
            "skipped": plan.stats["levels_scanned"] - plan.stats["edge_count"]}

    if budget_remaining is not None and plan.exposure > budget_remaining:
        print(f"\n  Budget check: proposed ${plan.exposure:.2f} exceeds remaining "
              f"${budget_remaining:.2f}. Cancelling batch.")
        return {"placed": 0, "failed": 0, "exposure": 0.0, "budget_exceeded": True, **base}
    if dry_run:
        return {"placed": 0, "failed": 0, "exposure": plan.exposure, "budget_exceeded": False, **base}

    result = execute_plan(client, plan, print_emit)
    print(f"\nDone. Placed {result.placed}, failed {result.failed}, skipped {base['skipped']}.")
    return {"placed": result.placed, "failed": result.failed,
            "exposure": result.filled_exposure, "budget_exceeded": False, **base}
