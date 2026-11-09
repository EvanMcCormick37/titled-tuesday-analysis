"""Titled Tuesday entry points for the helpers in src.kalshi_markets."""
from __future__ import annotations

from datetime import date
from typing import Optional, Union

import pandas as pd

from kalshi_core import KalshiClient

from .kalshi_markets import fetch_orderbooks, get_asks, get_event_markets, get_positions_df
from .markets import TITLED_TUESDAY

__all__ = ['fetch_orderbooks', 'get_tt_asks', 'get_tt_markets', 'get_tt_positions_df']

_TT_EVENT_TEMPLATES = list(TITLED_TUESDAY.event_templates)


def _next_tuesday(from_date: Optional[date] = None) -> date:
    return TITLED_TUESDAY.next_event_on_or_after(from_date)


def get_tt_markets(client: KalshiClient, event_date: Union[date, str, None] = None) -> list[dict]:
    return get_event_markets(client, TITLED_TUESDAY, event_date)


def get_tt_asks(
    client: KalshiClient, event_date: Union[date, str, None] = None, sleep: float = 0.05,
) -> list[dict]:
    """`sleep` is retained for call-site compatibility; orderbooks are fetched in bulk."""
    return get_asks(client, TITLED_TUESDAY, event_date)


def get_tt_positions_df(
    client: KalshiClient,
    event_date: Union[date, str, None] = None,
    sleep: float = 0.05,
    fees_by_ticker: Optional[dict[str, float]] = None,
) -> pd.DataFrame:
    return get_positions_df(client, TITLED_TUESDAY, event_date, fees_by_ticker)
