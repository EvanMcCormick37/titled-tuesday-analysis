"""Exchange gateways: turn OMS actions into Kalshi REST calls (or pretend to).

`LiveGateway` is the only place in the market maker that sends orders.
`ShadowGateway` acknowledges everything locally and sends nothing; tt-mm runs
it unless MM_LIVE=1. Both are synchronous; the engine calls them in threads.

Side conversion happens here: a bid on NO at q cents is Kalshi's YES-frame
'ask' at 100 − q.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .oms import Action, Order


@dataclass
class Result:
    ok: bool
    response: dict | None = None
    error: str | None = None
    status: int | None = None


def book_side(side: str) -> str:
    return 'bid' if side == 'yes' else 'ask'


def yes_price(side: str, price_c: int) -> int:
    return price_c if side == 'yes' else 100 - price_c


def _status_of(e: Exception) -> int | None:
    resp = getattr(e, 'response', None)
    return getattr(resp, 'status_code', None)


log = logging.getLogger(__name__)

CANCEL_CHUNK = 20          # Kalshi's per-request batch limit
CANCEL_PAUSE_S = 0.5       # 20 cancels = 40 write tokens; Basic tier refills 100/s


def cancel_in_chunks(client: Any, pairs: list[tuple[str, str | None]], *, chunk: int = CANCEL_CHUNK,
                     pause: float = CANCEL_PAUSE_S, retries: int = 6,
                     sleep: Callable[[float], None] = time.sleep) -> int:
    """Batch-cancel any number of (order_id, ticker) pairs without tripping the
    rate limit: `chunk` per request, paced, exponential backoff on 429. Returns
    how many were sent. Raises the last error once a chunk exhausts its retries."""
    sent = 0
    for i in range(0, len(pairs), chunk):
        part = pairs[i:i + chunk]
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                client.batch_cancel_orders(part)
                break
            except Exception as e:  # noqa: BLE001
                if _status_of(e) != 429 or attempt == retries:
                    raise
                log.warning('batch cancel throttled; retrying in %.0f s', delay)
                sleep(delay)
                delay = min(delay * 2, 30)
        sent += len(part)
        if i + chunk < len(pairs):
            sleep(pause)
    return sent


def _err(e: Exception) -> Result:
    resp = getattr(e, 'response', None)
    text = None
    try:
        text = resp.text[:300] if resp is not None else None
    except Exception:  # noqa: BLE001
        pass
    return Result(False, error=text or str(e), status=_status_of(e))


class LiveGateway:
    real = True

    def __init__(self, client: Any):
        self.client = client

    @staticmethod
    def _kwargs(a: Action, expiration_ts: int | None, group: str | None) -> dict:
        o = a.order
        return dict(ticker=o.ticker, book_side=book_side(o.side), yes_price_c=yes_price(o.side, a.price),
                    count=int(a.size), client_order_id=o.coid, expiration_ts=expiration_ts,
                    order_group_id=group)

    def execute(self, a: Action, expiration_ts: int | None = None, group: str | None = None) -> Result:
        o = a.order
        try:
            if a.kind == 'create':
                return Result(True, self.client.create_order(**self._kwargs(a, expiration_ts, group)))
            if a.kind == 'amend':
                return Result(True, self.client.amend_order(
                    o.order_id, o.ticker, book_side(o.side), yes_price(o.side, a.price), int(a.size)))
            if a.kind == 'decrease':
                return Result(True, self.client.decrease_order(o.order_id, int(a.size), o.ticker))
            if a.kind == 'cancel':
                return Result(True, self.client.cancel_order(o.order_id, o.ticker))
        except Exception as e:  # noqa: BLE001 — every failure is reported back to the OMS
            return _err(e)
        raise ValueError(a.kind)

    def batch_create(self, actions: list[Action], expiry: dict[str, int],
                     groups: dict[str, str | None]) -> list[Result]:
        bodies = [self.client.order_body(**self._kwargs(a, expiry.get(a.order.ticker),
                                                        groups.get(a.order.ticker)))
                  for a in actions]
        try:
            entries = self.client.batch_create_orders(bodies)
        except Exception as e:  # noqa: BLE001
            r = _err(e)
            # Whole-batch failure: a 4xx means nothing was placed; otherwise unknown.
            return [r] * len(actions)
        by_coid = {e.get('client_order_id'): e for e in entries}
        out = []
        for a, entry in zip(actions, entries if len(entries) == len(actions) else [None] * len(actions)):
            entry = by_coid.get(a.order.coid, entry)
            if entry is None:
                out.append(Result(False, error='missing from batch response'))
            elif entry.get('error'):
                err = entry['error']
                code = str(err.get('code', ''))
                ambiguous = 'timeout' in code.lower() or 'unavailable' in code.lower()
                out.append(Result(False, error=f"{code}: {err.get('message', '')}",
                                  status=None if ambiguous else 400))
            else:
                out.append(Result(True, entry))
        return out

    def batch_cancel(self, orders: list[Order]) -> list[Result]:
        try:
            entries = self.client.batch_cancel_orders([(o.order_id, o.ticker) for o in orders])
        except Exception as e:  # noqa: BLE001
            return [_err(e)] * len(orders)
        by_id = {e.get('order_id'): e for e in entries}
        out = []
        for o in orders:
            e = by_id.get(o.order_id)
            if e is None or e.get('error'):
                out.append(Result(False, error=str(e.get('error') if e else 'missing'), status=None))
            else:
                out.append(Result(True, e))
        return out


class ShadowGateway:
    """Acknowledges every action locally. Nothing reaches Kalshi."""
    real = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str, int | None, int | None]] = []

    def execute(self, a: Action, expiration_ts: int | None = None, group: str | None = None) -> Result:
        o = a.order
        self.sent.append((a.kind, o.ticker, o.side, o.tier, a.price, a.size))
        if a.kind == 'create':
            return Result(True, {'order_id': f'shadow-{uuid.uuid4().hex[:12]}',
                                 'remaining_count': f'{a.size:.2f}', 'client_order_id': o.coid})
        if a.kind in ('amend', 'decrease'):
            return Result(True, {'order_id': o.order_id, 'remaining_count': f'{a.size:.2f}'})
        return Result(True, {'order_id': o.order_id})

    def batch_create(self, actions: list[Action], expiry: dict[str, int],
                     groups: dict[str, str | None]) -> list[Result]:
        return [self.execute(a) for a in actions]

    def batch_cancel(self, orders: list[Order]) -> list[Result]:
        return [Result(True, {'order_id': o.order_id}) for o in orders]
