"""Order manager: the only component that decides which REST calls to make.

The strategy publishes desired quotes per slot = (ticker, side, tier). The OMS
holds what is live and computes the smallest set of actions to turn live into
target, within a local copy of Kalshi's token bucket:

    target, no live order          → create   (post_only, client_order_id = tier prefix + uuid)
    price differs, never filled    → amend    (price + count; Kalshi keeps the expiry)
    price differs, partly filled   → cancel, then create on the next pass
    same price, smaller            → decrease (keeps queue position)
    same price, larger             → amend    (loses queue position)
    live order, no target          → cancel

At most one request is in flight per slot; further changes wait in the target,
so a burst of book changes collapses into one action. Sides are "bid on YES"
and "bid on NO", each priced in its own cents; the gateway converts NO bids to
Kalshi's YES-frame 'ask' orders.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import ANCHOR_PREFIX, EXIT_PREFIX, MM_PREFIXES, SCOUT_PREFIX
from .quoting import Quote

ANCHOR, SCOUT, EXIT = 'anchor', 'scout', 'exit'
TIERS = (ANCHOR, SCOUT, EXIT)
_PREFIX = {ANCHOR: ANCHOR_PREFIX, SCOUT: SCOUT_PREFIX, EXIT: EXIT_PREFIX}
Slot = tuple[str, str, str]          # (ticker, side, tier)

# Order states
PENDING_NEW, RESTING, PENDING_AMEND, PENDING_CANCEL, UNKNOWN = (
    'pending_new', 'resting', 'pending_amend', 'pending_cancel', 'unknown')

COST = {'create': 10, 'amend': 10, 'decrease': 10, 'cancel': 2}


@dataclass
class Order:
    coid: str
    ticker: str
    side: str
    tier: str
    price: int
    remaining: float
    state: str = PENDING_NEW
    order_id: str | None = None
    real: bool = True                 # False in shadow mode: not in the exchange book
    filled: float = 0.0
    prev_price: int | None = None     # while an amend is in flight
    last_exchange_ts: int = 0
    executed: bool = False
    wipe_reported: bool = False
    missing_reconciles: int = 0
    pending_since: float = 0.0

    @property
    def slot(self) -> Slot:
        return (self.ticker, self.side, self.tier)


@dataclass
class Action:
    kind: str                         # create | amend | decrease | cancel
    order: Order
    price: int | None = None
    size: int | None = None
    priority: int = 5

    @property
    def cost(self) -> int:
        return COST[self.kind]


@dataclass
class FillEvent:
    tier: str
    ticker: str
    side: str
    price: int
    count: float
    remaining: float
    order_size: float
    fully_filled: bool
    coid: str
    trade_id: str


class TokenBucket:
    def __init__(self, capacity: float, refill_per_s: float, clock=time.monotonic):
        self.capacity = capacity
        self.refill = refill_per_s
        self.tokens = capacity
        self._clock = clock
        self._t = clock()

    def _update(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._t) * self.refill)
        self._t = now

    def available(self) -> float:
        self._update()
        return self.tokens

    def take(self, n: float) -> None:
        self._update()
        self.tokens -= n

    def shrink(self, factor: float = 0.7) -> None:
        """After a 429: trust the server, back off."""
        self.refill *= factor
        self.tokens = min(self.tokens, 0)


class OMS:
    def __init__(self, bucket: TokenBucket, *, real: bool = True, cancel_reserve: float = 20,
                 clock=time.monotonic, unknown_after_s: float = 10.0):
        self.bucket = bucket
        self.real = real
        self.cancel_reserve = cancel_reserve
        self._clock = clock
        self.unknown_after_s = unknown_after_s
        self.targets: dict[Slot, Quote | None] = {}
        self.orders: dict[Slot, Order] = {}
        self.by_coid: dict[str, Order] = {}
        self.by_order_id: dict[str, Order] = {}
        self.closed: dict[str, Order] = {}          # recently closed, for late fills
        self.seen_trades: set[str] = set()
        self.expiration_ts: dict[str, int] = {}     # ticker → expiry (the cutoff)
        self.order_group: dict[str, str | None] = {}  # ticker → order group id
        self.last_action_at: dict[str, float] = {}  # ticker → monotonic, for fair share
        # Our account's other resting orders (place_bids, fade, manual): order_id →
        # (ticker, side, price, remaining). Counted as "own" in the ex-own book so
        # the scout never outbids us.
        self.external: dict[str, tuple[str, str, int, float]] = {}
        self.stats = {'creates': 0, 'amends': 0, 'decreases': 0, 'cancels': 0, 'rejects': 0,
                      'throttled': 0, 'http_429': 0}

    # ── Targets ──────────────────────────────────────────────────────────────

    def set_target(self, slot: Slot, quote: Quote | None) -> None:
        self.targets[slot] = quote

    def clear_targets(self, tickers: set[str] | None = None) -> None:
        for slot in list(self.targets):
            if tickers is None or slot[0] in tickers:
                self.targets[slot] = None

    # ── Views ────────────────────────────────────────────────────────────────

    def own_levels(self, ticker: str, side: str) -> dict[int, float]:
        """Our resting size per price on (ticker, side), for the ex-own book.

        An order with an amend in flight is counted at both prices, so the book
        never mistakes our own order for a competitor while the amend lands.
        """
        out: dict[int, float] = {}
        for t, s, price, rem in list(self.external.values()):
            if t == ticker and s == side and self.real:
                out[price] = out.get(price, 0.0) + rem
        for tier in TIERS:
            o = self.orders.get((ticker, side, tier))
            if o is None or not o.real or o.state == PENDING_NEW:
                continue
            out[o.price] = out.get(o.price, 0.0) + o.remaining
            if o.state == PENDING_AMEND and o.prev_price is not None:
                out[o.prev_price] = out.get(o.prev_price, 0.0) + o.remaining
        return out

    def live_orders(self) -> list[Order]:
        return list(self.orders.values())

    def resting_notional(self) -> float:
        return sum(o.price * o.remaining / 100 for o in self.orders.values())

    # ── Planning ─────────────────────────────────────────────────────────────

    def _new_order(self, slot: Slot, q: Quote) -> Order:
        ticker, side, tier = slot
        o = Order(coid=f'{_PREFIX[tier]}{uuid.uuid4().hex[:20]}', ticker=ticker, side=side, tier=tier,
                  price=q.price, remaining=q.size, real=self.real)
        return o

    def _diff(self, slot: Slot) -> Action | None:
        target = self.targets.get(slot)
        live = self.orders.get(slot)
        if live is not None and live.state in (PENDING_NEW, PENDING_AMEND, PENDING_CANCEL, UNKNOWN):
            return None                                   # one request in flight per slot
        if live is None:
            if target is None:
                return None
            o = self._new_order(slot, target)
            # Anchors and exits first: they are the base quotes; scouts follow.
            return Action('create', o, target.price, target.size, priority=5 if slot[2] == SCOUT else 4)
        if target is None:
            return Action('cancel', live, priority=0)
        tsize = int(target.size)
        if target.price != live.price:
            outbid = slot[2] == SCOUT and target.price > live.price
            prio = 1 if outbid else (2 if slot[2] == SCOUT else 3)
            if live.filled > 0:
                return Action('cancel', live, priority=prio)   # recreate next pass
            return Action('amend', live, target.price, tsize, priority=prio)
        if tsize < live.remaining - 1e-9:
            return Action('decrease', live, live.price, tsize, priority=3)
        if tsize > live.remaining + 1e-9:
            if live.filled > 0:
                return Action('cancel', live, priority=6)
            return Action('amend', live, live.price, tsize, priority=6)
        return None

    def next_actions(self, max_actions: int = 50) -> list[Action]:
        """Actions to send now, highest priority first, within the token budget.

        Marks each chosen order as pending, so calling again won't duplicate it.
        Cancels may dip into the reserve; everything else leaves it untouched.
        """
        now = self._clock()
        for o in list(self.orders.values()):
            if o.state in (PENDING_NEW, PENDING_AMEND, PENDING_CANCEL) and o.pending_since \
                    and now - o.pending_since > self.unknown_after_s:
                o.state = UNKNOWN
        candidates = []
        for slot in set(self.targets) | set(self.orders):
            a = self._diff(slot)
            if a is not None:
                candidates.append(a)
        candidates.sort(key=lambda a: (a.priority, self.last_action_at.get(a.order.ticker, 0.0)))

        chosen: list[Action] = []
        for a in candidates:
            if len(chosen) >= max_actions:
                break
            reserve = 0 if a.kind == 'cancel' else self.cancel_reserve
            if self.bucket.available() - a.cost < reserve:
                self.stats['throttled'] += 1
                if a.kind == 'cancel':
                    break
                continue
            self.bucket.take(a.cost)
            self._mark_pending(a, now)
            self.last_action_at[a.order.ticker] = now
            chosen.append(a)
        return chosen

    def _mark_pending(self, a: Action, now: float) -> None:
        o = a.order
        o.pending_since = now
        if a.kind == 'create':
            o.state = PENDING_NEW
            self.orders[o.slot] = o
            self.by_coid[o.coid] = o
        elif a.kind == 'amend':
            o.state = PENDING_AMEND
            o.prev_price = o.price
            o.price, o.remaining = a.price, a.size
        elif a.kind == 'decrease':
            o.state = PENDING_AMEND
            o.remaining = a.size
        elif a.kind == 'cancel':
            o.state = PENDING_CANCEL

    # ── Results ──────────────────────────────────────────────────────────────

    def on_result(self, a: Action, ok: bool, response: dict | None = None,
                  error: str | None = None, status: int | None = None) -> None:
        o = a.order
        o.pending_since = 0.0
        if status == 429:
            self.stats['http_429'] += 1
            self.bucket.shrink()
        if a.kind == 'create':
            if ok and response and response.get('order_id'):
                o.order_id = response['order_id']
                self.by_order_id[o.order_id] = o
                remaining = response.get('remaining_count')
                if remaining is not None:
                    o.remaining = float(remaining)
                o.state = RESTING
                self.stats['creates'] += 1
                if o.remaining <= 0:
                    self._close(o)
            elif ok is False and status is not None and status < 500 and status != 429:
                self.stats['rejects'] += 1
                self._close(o)                       # definitively rejected
            else:
                o.state = UNKNOWN                    # timeout / 5xx: reconcile by client_order_id
        elif a.kind in ('amend', 'decrease'):
            if ok:
                o.state = RESTING
                o.prev_price = None
                if response and response.get('remaining_count') is not None:
                    o.remaining = float(response['remaining_count'])
                self.stats['amends' if a.kind == 'amend' else 'decreases'] += 1
                if o.remaining <= 0:
                    self._close(o)
            else:
                o.state = UNKNOWN
        elif a.kind == 'cancel':
            if ok or status == 404:
                self.stats['cancels'] += 1
                self._close(o)
            else:
                o.state = UNKNOWN

    def _close(self, o: Order) -> None:
        if self.orders.get(o.slot) is o:
            del self.orders[o.slot]
        self.closed[o.coid] = o
        if len(self.closed) > 5000:
            for k in list(self.closed)[:1000]:
                del self.closed[k]

    def _find(self, order_id: str | None, coid: str | None) -> Order | None:
        if coid and coid in self.by_coid:
            return self.by_coid[coid]
        if order_id and order_id in self.by_order_id:
            return self.by_order_id[order_id]
        return None

    # ── Exchange events ──────────────────────────────────────────────────────

    def on_user_order(self, msg: dict, is_ours=None) -> Order | None:
        o = self._find(msg.get('order_id'), msg.get('client_order_id'))
        if o is None:
            if is_ours is None or not is_ours(msg):
                self._track_external(msg)
            return None
        ts = int(msg.get('last_updated_ts_ms') or msg.get('created_ts_ms') or 0)
        if ts and ts < o.last_exchange_ts:
            return o                                  # out of date
        o.last_exchange_ts = ts or o.last_exchange_ts
        if msg.get('order_id') and not o.order_id:
            o.order_id = msg['order_id']
            self.by_order_id[o.order_id] = o
        status = msg.get('status')
        if msg.get('remaining_count_fp') is not None and o.state not in (PENDING_AMEND,):
            o.remaining = float(msg['remaining_count_fp'])
        if status == 'resting' and o.state in (PENDING_NEW, UNKNOWN):
            o.state = RESTING
        elif status in ('canceled', 'executed'):
            o.executed = status == 'executed'
            if o.state != PENDING_AMEND:
                self._close(o)
        return o

    def on_fill(self, msg: dict) -> FillEvent | None:
        trade_id = msg.get('trade_id')
        if trade_id in self.seen_trades:
            return None
        o = self._find(msg.get('order_id'), msg.get('client_order_id'))
        if o is None:
            o = self.closed.get(msg.get('client_order_id') or '')
        if o is None:
            return None
        if trade_id:
            self.seen_trades.add(trade_id)
        count = float(msg.get('count_fp') or msg.get('count') or 0)
        o.filled += count
        o.remaining = max(0.0, o.remaining - count)
        fully = o.executed or o.remaining <= 1e-9
        if fully and self.orders.get(o.slot) is o and o.state == RESTING:
            self._close(o)
        price = o.price
        yp = msg.get('yes_price_dollars')
        if yp is not None:
            yc = round(float(yp) * 100)
            price = yc if o.side == 'yes' else 100 - yc
        return FillEvent(o.tier, o.ticker, o.side, price, count, o.remaining,
                         o.filled + o.remaining, fully, o.coid, trade_id or '')

    def _track_external(self, msg: dict) -> None:
        oid = msg.get('order_id')
        if not oid:
            return
        if msg.get('status') != 'resting' or msg.get('action') == 'sell':
            self.external.pop(oid, None)
            return
        side = msg.get('outcome_side') or msg.get('side')
        if side not in ('yes', 'no'):
            return
        if side == 'no' and msg.get('no_price_dollars') is not None:
            price = round(float(msg['no_price_dollars']) * 100)
        elif msg.get('yes_price_dollars') is not None:
            yc = round(float(msg['yes_price_dollars']) * 100)
            price = yc if side == 'yes' else 100 - yc
        else:
            return
        rem = msg.get('remaining_count_fp', msg.get('remaining_count'))
        self.external[oid] = (msg.get('ticker', ''), side, price, float(rem or 0))

    # ── Reconciliation against REST ──────────────────────────────────────────

    def reconcile(self, open_orders: list[dict], is_ours) -> list[dict]:
        """Sync with REST open orders. Returns our exchange orders the OMS doesn't
        know about (orphans), which the caller should cancel. Also refreshes the
        account's other resting orders."""
        self.external = {}
        for r in open_orders:
            if not is_ours(r) and not str(r.get('client_order_id') or '').startswith(MM_PREFIXES):
                self._track_external({**r, 'status': 'resting'})
        rest_by_coid = {o.get('client_order_id'): o for o in open_orders if is_ours(o)}
        for o in list(self.orders.values()):
            r = rest_by_coid.get(o.coid)
            if r is not None:
                o.missing_reconciles = 0
                o.order_id = o.order_id or r.get('order_id')
                if o.order_id:
                    self.by_order_id[o.order_id] = o
                if o.state == UNKNOWN:
                    o.state = RESTING
                    o.prev_price = None
                    yc = r.get('yes_price_dollars')
                    if yc is not None:
                        c = round(float(yc) * 100)
                        o.price = c if o.side == 'yes' else 100 - c
                rem = r.get('remaining_count_fp', r.get('remaining_count'))
                if rem is not None and o.state == RESTING:
                    o.remaining = float(rem)
            elif o.state in (RESTING, UNKNOWN):
                o.missing_reconciles += 1
                if o.state == UNKNOWN or o.missing_reconciles >= 2:
                    self._close(o)
        known = {o.coid for o in self.orders.values()}
        return [r for c, r in rest_by_coid.items() if c not in known]
