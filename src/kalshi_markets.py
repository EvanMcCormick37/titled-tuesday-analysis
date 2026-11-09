"""Kalshi helpers for any weekly top-N market, driven by its MarketSpec."""
from __future__ import annotations

from datetime import date
from typing import Optional, Union

import pandas as pd

from kalshi_core import KalshiClient, _to_kalshi_date

from .markets import MarketSpec


def get_event_markets(
    client: KalshiClient, spec: MarketSpec, event_date: Union[date, str, None] = None,
) -> list[dict]:
    """All open contracts for an event date (default: the next one), each with its `n`."""
    if event_date is None:
        event_date = spec.next_event_on_or_after()
    date_str = _to_kalshi_date(event_date)
    return [{**m, 'n': n}
            for tmpl, n in spec.event_templates.items()
            for m in client.get_markets_by_event(tmpl.format(date=date_str))]


def fetch_orderbooks(client: KalshiClient, tickers: list[str]) -> dict[str, dict]:
    """Bulk-fetch orderbooks, retrying any ticker the batch response left out.

    A ticker that still can't be fetched is absent from the result, so callers
    can skip it instead of mistaking it for an empty book.
    """
    books = client.get_orderbooks(tickers) if tickers else {}
    for ticker in tickers:
        if ticker in books:
            continue
        try:
            books[ticker] = client.get_orderbook(ticker)
        except Exception as e:
            print(f"Could not retrieve orderbook for {ticker}: {e}")
    return books


def get_asks(
    client: KalshiClient, spec: MarketSpec, event_date: Union[date, str, None] = None,
) -> list[dict]:
    """Every resting ask level for an event date.

    Each entry: Market Ticker, Market Title, Player Name, n,
                Ask Price ($), Quantity Available, Side ('YES' | 'NO')
    """
    markets = get_event_markets(client, spec, event_date)
    books = fetch_orderbooks(client, [m["ticker"] for m in markets])
    all_asks = []
    for market in markets:
        ob = books.get(market["ticker"])
        if ob is None:
            continue
        base = {"Market Ticker": market["ticker"], "Market Title": market.get("title", ""),
                "Player Name": market.get("yes_sub_title", ""), "n": market["n"]}
        for price_str, qty_str in ob.get("yes_dollars") or []:
            all_asks.append({**base, "Ask Price ($)": 1.0 - float(price_str),
                             "Quantity Available": float(qty_str), "Side": "NO"})
        for price_str, qty_str in ob.get("no_dollars") or []:
            all_asks.append({**base, "Ask Price ($)": 1.0 - float(price_str),
                             "Quantity Available": float(qty_str), "Side": "YES"})
    return all_asks


def get_positions_df(
    client: KalshiClient,
    spec: MarketSpec,
    event_date: Union[date, str, None] = None,
    fees_by_ticker: Optional[dict[str, float]] = None,
) -> pd.DataFrame:
    """Live positions for an event date, with fee-inclusive cost basis.

    Pass `fees_by_ticker` to supply fees directly instead of reading the local
    kalshi-core `fills` DB. `cost` and `avg_price` are Kalshi's raw exposure
    numbers; `cost_incl_fees` and `avg_price_incl_fees` add the fees.

    Returns columns: ticker, marketTitle, n, position, volume, cost, fees,
    cost_incl_fees, avg_price, avg_price_incl_fees.
    """
    if event_date is None:
        event_date = spec.next_event_on_or_after()
    date_str = _to_kalshi_date(event_date)

    positions_df = client.get_positions()
    if positions_df.empty:
        return pd.DataFrame()

    pattern = spec.ticker_pattern(date_str)
    held = positions_df[positions_df["ticker"].apply(lambda t: bool(pattern.search(t)))].copy()
    if held.empty:
        return pd.DataFrame()

    pos_col = "position_fp"
    exp_col = "market_exposure_dollars"
    for col in (pos_col, exp_col):
        if col not in held.columns:
            raise KeyError(f"Expected '{col}' in positions; got {list(held.columns)}")

    held = held[held[pos_col].astype(float) != 0].copy()
    if held.empty:
        return pd.DataFrame()

    held["n"] = held["ticker"].apply(lambda t: spec.n_from_ticker(t, date_str))
    held = held[held["n"].notna()].copy()
    held["n"] = held["n"].astype(int)

    signed = held[pos_col].astype(float)
    held["position"] = signed.apply(lambda p: "Yes" if p > 0 else "No")
    held["volume"]   = signed.abs()
    held["cost"]     = held[exp_col].astype(float)
    held["avg_price"] = held["cost"] / held["volume"]

    # kalshi_core.db.get_fills reads the central ~/.kalshi/data/kalshi.db populated
    # by sync_fills(client).
    if fees_by_ticker is None:
        fees_by_ticker = {}
        try:
            from kalshi_core import get_fills
            fills = get_fills()
            if not fills.empty and "fee_cost" in fills.columns:
                fees_by_ticker = fills.groupby("ticker")["fee_cost"].sum().to_dict()
        except Exception as e:
            print(f"  Warning: could not load fills for fee lookup: {e}")

    held["fees"] = held["ticker"].map(fees_by_ticker).fillna(0.0).astype(float)
    held["cost_incl_fees"]      = held["cost"] + held["fees"]
    held["avg_price_incl_fees"] = held["cost_incl_fees"] / held["volume"]

    market_titles = {
        m["ticker"]: m.get("yes_sub_title", "")
        for m in client.get_markets_by_tickers(held["ticker"].tolist())
    }
    held["marketTitle"] = held["ticker"].map(market_titles).fillna("")
    return held[["ticker", "marketTitle", "n", "position", "volume",
                 "cost", "fees", "cost_incl_fees",
                 "avg_price", "avg_price_incl_fees"]].reset_index(drop=True)
