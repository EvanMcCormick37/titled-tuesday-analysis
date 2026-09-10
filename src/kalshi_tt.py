"""Titled Tuesday-specific Kalshi market helpers."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Optional, Union

import pandas as pd

from kalshi_core import KalshiClient, _to_kalshi_date

_TT_EVENT_TEMPLATES = [
    "KXTITLEDTUESDAY-{date}",    # winner
    "KXTITLEDTUESTOP-{date}T3",  # top 3
    "KXTITLEDTUESTOP-{date}T5",  # top 5
    "KXTITLEDTUESTOP-{date}T8",  # top 8
]


def _next_tuesday(from_date: Optional[date] = None) -> date:
    d = from_date or date.today()
    days_until = (1 - d.weekday()) % 7  # 0 if today is Tuesday
    return d + timedelta(days=days_until)


def get_tt_markets(
    client: KalshiClient, event_date: Union[date, str, None] = None
) -> list[dict]:
    """Return all open markets for a Titled Tuesday date (defaults to soonest upcoming Tuesday)."""
    if event_date is None:
        event_date = _next_tuesday()
    date_str = _to_kalshi_date(event_date)
    markets = []
    for tmpl in _TT_EVENT_TEMPLATES:
        markets.extend(client.get_markets_by_event(tmpl.format(date=date_str)))
    return markets


def get_tt_asks(
    client: KalshiClient,
    event_date: Union[date, str, None] = None,
    sleep: float = 0.05,
) -> list[dict]:
    """Fetch all resting ask levels for a Titled Tuesday event date.

    Each entry: Market Ticker, Market Title, Player Name,
                Ask Price ($), Quantity Available, Side ('YES' | 'NO')
    """
    markets = get_tt_markets(client, event_date)
    all_asks = []
    for market in markets:
        ticker   = market["ticker"]
        title    = market.get("title", "")
        subtitle = market.get("yes_sub_title", "")
        try:
            ob = client.get_orderbook(ticker)
            for price_str, qty_str in ob.get("yes_dollars") or []:
                all_asks.append({
                    "Market Ticker": ticker, "Market Title": title, "Player Name": subtitle,
                    "Ask Price ($)": 1.0 - float(price_str),
                    "Quantity Available": float(qty_str), "Side": "NO",
                })
            for price_str, qty_str in ob.get("no_dollars") or []:
                all_asks.append({
                    "Market Ticker": ticker, "Market Title": title, "Player Name": subtitle,
                    "Ask Price ($)": 1.0 - float(price_str),
                    "Quantity Available": float(qty_str), "Side": "YES",
                })
        except Exception as e:
            print(f"Could not retrieve orderbook for {ticker}: {e}")
        if sleep:
            time.sleep(sleep)
    return all_asks


def get_tt_positions_df(
    client: KalshiClient,
    event_date: Union[date, str, None] = None,
    sleep: float = 0.05,
) -> pd.DataFrame:
    """Fetch current TT positions from Kalshi API.

    Returns columns: ticker, marketTitle, n, position, volume, cost, avg_price.
    """
    if event_date is None:
        event_date = _next_tuesday()
    date_str = _to_kalshi_date(event_date)

    positions_df = client.get_positions()
    if positions_df.empty:
        return pd.DataFrame()

    tt_pat = re.compile(
        rf"(?:KXTITLEDTUESDAY-{date_str}|KXTITLEDTUESTOP-{date_str}T\d+)-"
    )
    mask = positions_df["ticker"].apply(lambda t: bool(tt_pat.search(t)))
    tt = positions_df[mask].copy()
    if tt.empty:
        return pd.DataFrame()

    pos_col = "position_fp"
    exp_col = "market_exposure_dollars"
    for col in (pos_col, exp_col):
        if col not in tt.columns:
            raise KeyError(f"Expected '{col}' in positions; got {list(tt.columns)}")

    tt = tt[tt[pos_col].astype(float) != 0].copy()
    if tt.empty:
        return pd.DataFrame()

    def _n(ticker: str) -> Optional[int]:
        if f"KXTITLEDTUESDAY-{date_str}-" in ticker:
            return 1
        m = re.search(rf"KXTITLEDTUESTOP-{date_str}T(\d+)-", ticker)
        return int(m.group(1)) if m else None

    tt["n"] = tt["ticker"].apply(_n)
    tt = tt[tt["n"].notna()].copy()
    tt["n"] = tt["n"].astype(int)

    signed = tt[pos_col].astype(float)
    tt["position"] = signed.apply(lambda p: "Yes" if p > 0 else "No")
    tt["volume"]   = signed.abs()
    tt["cost"]     = tt[exp_col].astype(float)
    tt["avg_price"] = tt["cost"] / tt["volume"]

    market_titles: dict[str, str] = {}
    for ticker in tt["ticker"]:
        try:
            mkt = client.get_market(ticker).get("market", {})
            market_titles[ticker] = mkt.get("yes_sub_title", "")
        except Exception as e:
            print(f"  Warning: could not fetch {ticker}: {e}")
            market_titles[ticker] = ""
        if sleep:
            time.sleep(sleep)

    tt["marketTitle"] = tt["ticker"].map(market_titles)
    return tt[["ticker", "marketTitle", "n", "position", "volume", "cost", "avg_price"]].reset_index(drop=True)
