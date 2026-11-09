"""Builders for the live Kalshi snapshots the worker publishes to the dashboard."""
from __future__ import annotations

import json
import time
from typing import Any

from . import trading
from .kalshi_tt import get_tt_positions_df


def _records(df: Any) -> list[dict]:
    """DataFrame to JSON-safe records (NaN becomes null)."""
    return json.loads(df.to_json(orient='records'))


def tt_edges_snapshot(client: Any, tourn_date: str) -> dict:
    df, unmatched = trading.compute_edges(client, tourn_date, sleep_read=0.05)
    rows = []
    if not df.empty:
        out = df[['Market Ticker', 'Player Name', 'n', 'Side', 'Ask Price ($)', 'Quantity Available',
                  'EV', 'Order Price', 'Total Cost', 'Expected Return', 'ROI']].copy()
        out.columns = ['ticker', 'player', 'n', 'side', 'ask', 'qty', 'ev', 'order_price',
                       'total_cost', 'expected_return', 'roi']
        rows = _records(out)
    return {'tourn_date': tourn_date, 'rows': rows, 'unmatched': unmatched}


def _fees_by_ticker(client: Any, tickers: list[str], sleep: float = 0.05) -> dict[str, float]:
    """Sum fees from Kalshi directly; the container's local fills DB doesn't persist."""
    fees: dict[str, float] = {}
    for ticker in tickers:
        fees[ticker] = sum(float(f.get('fee_cost', 0) or 0) for f in client.get_fills(ticker))
        if sleep:
            time.sleep(sleep)
    return fees


def tt_positions_snapshot(client: Any, tourn_date: str) -> dict:
    tickers = client.get_positions()
    held = [] if tickers.empty else tickers['ticker'].tolist()
    from kalshi_core import _to_kalshi_date
    kdate = _to_kalshi_date(tourn_date)
    relevant = [t for t in held if kdate in t and t.startswith('KXTITLEDTUES')]
    df = get_tt_positions_df(client, tourn_date, fees_by_ticker=_fees_by_ticker(client, relevant))
    orders = [o for o in client.get_open_orders()
              if kdate in o.get('ticker', '') and o.get('ticker', '').startswith('KXTITLEDTUES')]
    resting = [{
        'ticker': o.get('ticker'),
        'side': o.get('outcome_side') or o.get('side'),
        'price_c': trading._order_cents(o),
        'qty': o.get('remaining_count', o.get('count')),
        'expires': o.get('expiration_time'),
    } for o in orders]
    return {'tourn_date': tourn_date, 'rows': [] if df.empty else _records(df), 'orders': resting}
