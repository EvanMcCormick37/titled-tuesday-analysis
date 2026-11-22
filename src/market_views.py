"""Builders for the live Kalshi snapshots the worker publishes to the dashboard."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from kalshi_core import _to_kalshi_date

from . import job_queue as q
from . import trading
from .db import read_market_rows
from .kalshi_markets import get_positions_df
from .markets import MarketSpec


def _records(df: Any) -> list[dict]:
    """DataFrame to JSON-safe records (NaN becomes null)."""
    return json.loads(df.to_json(orient='records'))


def edges_snapshot(client: Any, tourn_date: str, spec: MarketSpec) -> dict:
    df, unmatched = trading.compute_edges(client, tourn_date, sleep_read=0.05, spec=spec)
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


def positions_snapshot(client: Any, tourn_date: str, spec: MarketSpec) -> dict:
    tickers = client.get_positions()
    held = [] if tickers.empty else tickers['ticker'].tolist()
    pattern = spec.ticker_pattern(_to_kalshi_date(tourn_date))
    relevant = [t for t in held if pattern.search(t)]
    df = get_positions_df(client, spec, tourn_date, fees_by_ticker=_fees_by_ticker(client, relevant))
    orders = [o for o in client.get_open_orders() if pattern.search(o.get('ticker', ''))]
    resting = [{
        'ticker': o.get('ticker'),
        'side': o.get('outcome_side') or o.get('side'),
        'price_c': trading._order_cents(o),
        'qty': o.get('remaining_count', o.get('count')),
        'expires': o.get('expiration_time'),
    } for o in orders]
    return {'tourn_date': tourn_date, 'rows': [] if df.empty else _records(df), 'orders': resting}


PNL_TIMEOUT_S = 180


def _pnl_input_key(df: Any, spec: MarketSpec, tourn_date: str) -> str:
    """Hash of everything the simulation reads: positions, costs and official predictions."""
    preds = read_market_rows('latest_model_predictions', spec.slug)
    pos = df[['ticker', 'volume', 'cost_incl_fees']].sort_values('ticker').round(4)
    h = hashlib.sha1(f'{tourn_date}|{len(preds)}'.encode())
    h.update(pos.to_csv(index=False).encode())
    if not preds.empty:
        h.update(preds.sort_values('username').round(6).to_csv(index=False).encode())
    return h.hexdigest()[:16]
_PNL_SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'portfolio_pnl.py'


def pnl_snapshot(client: Any, tourn_date: str, spec: MarketSpec) -> dict:
    """Monte Carlo P&L distribution of the held positions for the Portfolio page.

    Positions are read here with the worker's client; the simulation runs in a
    subprocess (scripts/portfolio_pnl.py) so its CPU use can't stall orders.
    """
    tickers = client.get_positions()
    held = [] if tickers.empty else tickers['ticker'].tolist()
    pattern = spec.ticker_pattern(_to_kalshi_date(tourn_date))
    relevant = [t for t in held if pattern.search(t)]
    df = get_positions_df(client, spec, tourn_date, fees_by_ticker=_fees_by_ticker(client, relevant))
    if df.empty:
        return {'tourn_date': tourn_date, 'empty': True}

    # The run takes ~40 s, so reuse the last result while positions and predictions are unchanged.
    key = _pnl_input_key(df, spec, tourn_date)
    previous = (q.get_snapshot(spec.slug, 'pnl') or {}).get('payload') or {}
    if previous.get('input_key') == key:
        return previous

    proc = subprocess.run(
        [sys.executable, str(_PNL_SCRIPT), f'--market={spec.slug}', f'--date={tourn_date}'],
        input=json.dumps(_records(df)), capture_output=True, text=True, timeout=PNL_TIMEOUT_S,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()[-1:] or ['no output']
        raise RuntimeError(f'P&L simulation failed: {tail[0]}')
    return {**json.loads(proc.stdout), 'input_key': key}
