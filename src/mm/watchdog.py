"""Watchdog: runs inside tt-worker, a separate process from tt-mm.

Two jobs, both touching only market-maker orders (client_order_id prefixes):

  1. Cutoff, layer 4. From one minute after an event's cutoff, any market-maker
     order still resting on that event is cancelled and an alert raised. (Layer 1
     is the order expiry itself, 2 the strategy clock, 3 tt-mm's own cancel-all.)
  2. Dead-man's switch. If tt-mm's heartbeat is older than HEARTBEAT_STALE_S,
     its scouts and exits are cancelled: they are priced off a book (exits, at fair
     with no discount) that nobody is watching.
     Anchors are priced off fair value alone, so they stay, and expire at the cutoff.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from kalshi_core import _to_kalshi_date

from . import store
from .config import EXIT_PREFIX, SCOUT_PREFIX, is_mm_order
from .gateway import cancel_in_chunks

log = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 20
CUTOFF_GRACE_S = 60


def _cutoff_ts(market: Any, event_date: str) -> int:
    params = store.params_for(market.slug)
    return market.spec.start_ts(event_date) - params.cutoff_minutes_before_start * 60


def check(client: Any, markets: dict, now: float | None = None) -> dict:
    """One watchdog pass. Returns counts of what it cancelled, per market."""
    now = now if now is not None else time.time()
    open_orders = None
    report: dict[str, dict] = {}
    for market in markets.values():
        cancelled_cutoff = cancelled_scouts = 0
        try:
            event_date = market.next_event_date()
        except Exception:  # noqa: BLE001 — no standings yet etc.
            continue
        dates = [event_date, market.previous_event_date(event_date)]
        past_cutoff = [d for d in dates if now >= _cutoff_ts(market, d) + CUTOFF_GRACE_S]
        age = store.heartbeat_age(market.slug)
        status = store.get_status(market.slug)
        mm_live = bool(status and status.get('mode') == 'live')
        stale = mm_live and (age is None or age > HEARTBEAT_STALE_S)
        if not past_cutoff and not stale:
            continue
        if open_orders is None:
            open_orders = [o for o in client.get_open_orders() if is_mm_order(o)]

        if past_cutoff:
            patterns = [market.spec.ticker_pattern(_to_kalshi_date(d)) for d in past_cutoff]
            late = [o for o in open_orders if any(p.search(o.get('ticker', '')) for p in patterns)]
            if late:
                cancel_in_chunks(client, [(o['order_id'], o.get('ticker')) for o in late])
                cancelled_cutoff = len(late)
                store.add_alert(market.slug, 'watchdog_cutoff',
                                f'Watchdog cancelled {len(late)} market-maker order(s) still resting '
                                'after the cutoff', level='error', tourn_date=event_date)
        if stale:
            pattern = market.spec.ticker_pattern(_to_kalshi_date(event_date))
            scouts = [o for o in open_orders if str(o.get('client_order_id', '')).startswith((SCOUT_PREFIX, EXIT_PREFIX))
                      and pattern.search(o.get('ticker', ''))]
            if scouts:
                cancel_in_chunks(client, [(o['order_id'], o.get('ticker')) for o in scouts])
                cancelled_scouts = len(scouts)
                store.add_alert(market.slug, 'watchdog_heartbeat',
                                f'tt-mm heartbeat is {"missing" if age is None else f"{age:.0f} s old"}; '
                                f'cancelled {len(scouts)} scout/exit order(s). Anchors stay until the cutoff.',
                                level='error', tourn_date=event_date)
        report[market.slug] = {'cutoff': cancelled_cutoff, 'scouts': cancelled_scouts}
    return report


def wait_for_pause_ack(market: str, timeout: float = 10.0) -> bool:
    """Used by the kill switch: give a live tt-mm a moment to stop quoting before
    sweeping, so it can't re-post behind the sweep. True if acknowledged or idle."""
    age = store.heartbeat_age(market)
    if age is None or age > HEARTBEAT_STALE_S:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = store.get_status(market)
        if st and st.get('paused_ack'):
            return True
        time.sleep(0.5)
    return False

