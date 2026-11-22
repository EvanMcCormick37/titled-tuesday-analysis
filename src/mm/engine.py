"""The market-maker process: one asyncio loop for every registered market.

    WS msg → seq check → Book.apply → mark market dirty
    quote tick (every 50 ms, dirty markets only):
        quote_side per (ticker, side) → risk.check → dwell/price-war filter → OMS targets
    exec tick (every 20 ms): OMS.next_actions → gateway (threads) → OMS.on_result
    slow tick (every 1 s): control + params, cutoff, status, model runs, discovery,
        reconciliation, positions

All markets share one OMS and so one token bucket: Kalshi's rate limit is per
account, and Titled Tuesday's and Bullet Brawl's quoting windows overlap.

The engine only ever touches orders whose client_order_id carries a
market-maker prefix (config.MM_PREFIXES), so fade and manual orders are safe.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from kalshi_core import _to_kalshi_date

from ..markets import MarketSpec
from . import fair_value, risk, store
from .book import SIDES, Book
from .config import MMParams, is_mm_order
from .journal import Journal
from .oms import ANCHOR, OMS, SCOUT, Action, FillEvent, Slot, TokenBucket
from .quoting import Quote, quote_side, side_fair

log = logging.getLogger(__name__)

PRIVATE_CHANNELS = ['fill', 'user_orders', 'order_group_updates']


@dataclass
class Contract:
    ticker: str
    market: str
    n: int
    subtitle: str
    username: str | None


@dataclass
class MarketState:
    spec: MarketSpec
    event_date: str | None = None
    prior_date: str | None = None
    t_cut: int = 0
    fv: fair_value.FairValueSet | None = None
    fv_errors: list[str] = field(default_factory=list)
    params: MMParams = field(default_factory=MMParams)
    paused: bool = False
    pause_reason: str | None = None
    overrides: dict[str, dict] = field(default_factory=dict)
    tickers: set[str] = field(default_factory=set)
    unmatched: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    group_id: str | None = None
    cutoff_done: bool = False
    held: bool = False                 # model run failed validation


def _to_thread(fn: Callable, *args: Any, **kw: Any):
    return asyncio.to_thread(fn, *args, **kw)


class Engine:
    def __init__(
        self,
        specs: list[MarketSpec],
        client: Any,
        gateway: Any,
        *,
        ws_factory: Callable[[], Any] | None = None,
        scouts: bool = True,
        journal: Journal | None = None,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        write_budget: tuple[float, float] | None = None,
        exec_concurrency: int = 4,
    ):
        self.client = client
        self.gateway = gateway
        self.live = bool(getattr(gateway, 'real', False))
        self.scouts = scouts
        self.ws_factory = ws_factory
        self.journal = journal or Journal(enabled=False)
        self.wall, self.mono = wall, mono
        cap, refill = write_budget or (100.0, 100.0)
        # 80% of the published budget leaves headroom for the dashboard and the watchdog.
        self.oms = OMS(TokenBucket(cap * 0.8, refill * 0.8, clock=mono), real=self.live, clock=mono)
        self.markets: dict[str, MarketState] = {s.slug: MarketState(s) for s in specs}
        self.contracts: dict[str, Contract] = {}
        self.books: dict[str, Book] = {}
        self.positions: dict[str, float] = defaultdict(float)
        self.dirty: set[str] = set()              # market slugs needing a quote pass
        self.ws_connected = False
        self._ws = None
        self._sids: dict[str, int] = {}
        self._seq: dict[int, int] = {}
        self._subscribed: set[str] = set()
        self._dwell: dict[Slot, tuple[Quote | None, float]] = {}
        self._reprices: dict[tuple[str, str], deque] = defaultdict(deque)
        self._war_until: dict[tuple[str, str], float] = {}
        self._sem = asyncio.Semaphore(exec_concurrency)
        self._inflight: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._last: dict[str, float] = defaultdict(float)
        self.last_quotes: dict[str, dict] = {}

    # ── Helpers ──────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def _market_of(self, ticker: str) -> MarketState | None:
        c = self.contracts.get(ticker)
        return self.markets.get(c.market) if c else None

    def _player_key(self, c: Contract) -> str:
        return c.username or c.subtitle

    def _alert(self, ms: MarketState, kind: str, message: str, level: str = 'info', **kw: Any) -> None:
        log.log(logging.WARNING if level != 'info' else logging.INFO, '[%s] %s', ms.spec.slug, message)
        self.journal.write('alert', {'market': ms.spec.slug, 'kind': kind, 'message': message, **kw})
        try:
            store.add_alert(ms.spec.slug, kind, message, tourn_date=ms.event_date, level=level, **kw)
        except Exception:  # noqa: BLE001
            log.exception('could not store alert')

    def _quoting_window_open(self, ms: MarketState) -> bool:
        # Stop 5 s early: an order created at the cutoff would be born expired.
        return ms.event_date is not None and self.wall() < ms.t_cut - 5

    # ── Discovery and fair values (slow path) ────────────────────────────────

    def refresh_event(self, ms: MarketState) -> None:
        """Work out which event to quote and its cutoff. Resets state on a new event."""
        from ..pipeline import next_tourn_date
        from ..strategies import _previous
        event_date = next_tourn_date(ms.spec)
        if event_date != ms.event_date:
            if ms.event_date is not None:
                self.oms.clear_targets(ms.tickers)
            ms.event_date = event_date
            ms.prior_date = _previous(event_date)
            ms.t_cut = ms.spec.start_ts(event_date) - ms.params.cutoff_minutes_before_start * 60
            ms.fv, ms.cutoff_done, ms.group_id, ms.held = None, False, None, False
            log.info('[%s] quoting event %s, cutoff %s', ms.spec.slug, event_date,
                     datetime.fromtimestamp(ms.t_cut, timezone.utc).isoformat())

    def discover(self, ms: MarketState) -> None:
        """List the event's Kalshi markets and map each to a model username."""
        if ms.event_date is None:
            return
        from ..data import market_player_to_username, match_player
        kdate = _to_kalshi_date(ms.event_date)
        rows = []
        for tmpl, n in ms.spec.event_templates.items():
            for m in self.client.get_markets_by_event(tmpl.format(date=kdate)):
                rows.append((m, n))
        name_map = market_player_to_username(ms.fv.preds.index, ms.spec) if ms.fv is not None else {}
        tickers, unmatched, skipped = set(), [], []
        for m, n in rows:
            ticker, subtitle = m['ticker'], m.get('yes_sub_title', '')
            status = m.get('status')
            if status and status not in ('active', 'open', 'initialized'):
                skipped.append({'ticker': ticker, 'reason': f'status {status}'})
                continue
            tick = m.get('tick_size')
            if tick not in (None, 1):
                skipped.append({'ticker': ticker, 'reason': f'tick size {tick}'})
                continue
            username = match_player(subtitle, name_map) if name_map else None
            if username is None:
                unmatched.append(subtitle)
            self.contracts[ticker] = Contract(ticker, ms.spec.slug, n, subtitle, username)
            self.books.setdefault(ticker, Book(ticker))
            tickers.add(ticker)
        removed = ms.tickers - tickers
        if removed:
            self.oms.clear_targets(removed)
        ms.tickers, ms.unmatched, ms.skipped = tickers, sorted(set(unmatched)), skipped
        self.ensure_order_group(ms)
        for t in tickers:
            self.oms.expiration_ts[t] = ms.t_cut
            self.oms.order_group[t] = ms.group_id
        self.dirty.add(ms.spec.slug)

    def reload_fair_values(self, ms: MarketState) -> bool:
        """Swap in a new validated model run. Returns True if the version changed."""
        if ms.event_date is None:
            return False
        fv, errors = fair_value.load(ms.spec, ms.event_date, ms.prior_date)
        if fv is None:
            if errors != ms.fv_errors:
                hard = not any(e.startswith(('pipeline', 'recompute')) for e in errors)
                if hard:
                    ms.held = True
                    self._alert(ms, 'model_invalid', f'Model run failed checks; holding: {"; ".join(errors)}',
                                level='error')
            ms.fv_errors = errors
            return False
        ms.fv_errors, ms.held = [], False
        if ms.fv is not None and fv.version == ms.fv.version:
            return False
        first = ms.fv is None
        ms.fv = fv
        cleared = store.clear_factors(ms.spec.slug, ms.event_date, fv.version)
        if not first or cleared:
            self._alert(ms, 'model_run', f'New model run {fv.version}; repricing every quote'
                        + (f', cleared {cleared} fallback shift(s)' if cleared else ''))
        return True

    # ── WebSocket ────────────────────────────────────────────────────────────

    def all_tickers(self) -> list[str]:
        return sorted(t for ms in self.markets.values() for t in ms.tickers)

    def mark_all_stale(self) -> None:
        for b in self.books.values():
            b.stale = True
        self.dirty.update(self.markets)

    async def _sync_subscription(self) -> None:
        if self._ws is None or 'orderbook_delta' not in self._sids:
            return
        want = set(self.all_tickers())
        sid = self._sids['orderbook_delta']
        add, drop = sorted(want - self._subscribed), sorted(self._subscribed - want)
        if add:
            await self._ws.add_markets(sid, add)
        if drop:
            await self._ws.delete_markets(sid, drop)
        self._subscribed = want

    async def ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with self.ws_factory() as ws:
                    self._ws, self._sids, self._seq = ws, {}, {}
                    tickers = self.all_tickers()
                    if tickers:
                        await ws.subscribe(['orderbook_delta'], market_tickers=tickers)
                    self._subscribed = set(tickers)
                    await ws.subscribe(PRIVATE_CHANNELS)
                    self.ws_connected = True
                    backoff = 1.0
                    async for msg in ws:
                        self.handle(msg)
                        if self._stop.is_set():
                            break
                        if 'resnapshot' in self._last and self._last.pop('resnapshot'):
                            sid = self._sids.get('orderbook_delta')
                            if sid is not None:
                                await ws.get_snapshot(sid, self.all_tickers())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — any WS failure means reconnect
                log.warning('websocket dropped: %s', e)
            self.ws_connected, self._ws = False, None
            self.mark_all_stale()                 # scouts come down; anchors stay
            await asyncio.sleep(backoff * (0.5 + (time.time() % 1)))
            backoff = min(backoff * 2, 30)

    def handle(self, msg: dict) -> None:
        self.journal.write('ws', msg)
        typ = msg.get('type')
        body = msg.get('msg') or {}
        if typ == 'subscribed':
            self._sids[body.get('channel')] = body.get('sid')
            return
        sid, seq = msg.get('sid'), msg.get('seq')
        if sid is not None and seq is not None:
            last = self._seq.get(sid)
            self._seq[sid] = seq
            if last is not None and seq != last + 1 and typ != 'orderbook_snapshot':
                log.warning('seq gap on sid %s: %s → %s', sid, last, seq)
                if typ == 'orderbook_delta':
                    self.mark_all_stale()
                    self._last['resnapshot'] = 1
                else:
                    self._last['reconcile'] = 0      # force a REST reconcile soon
        if typ == 'orderbook_snapshot':
            book = self.books.get(body.get('market_ticker'))
            if book is not None:
                book.apply_snapshot(body)
                self._touch(book.ticker)
        elif typ == 'orderbook_delta':
            book = self.books.get(body.get('market_ticker'))
            if book is not None:
                book.apply_delta(body)
                self._touch(book.ticker)
        elif typ == 'user_order':
            o = self.oms.on_user_order(body, is_mm_order)
            if o is not None:
                self._touch(o.ticker)
        elif typ == 'fill':
            ev = self.oms.on_fill(body)
            if ev is not None:
                self.on_fill(ev)
        elif typ == 'order_group_updates' or 'order_group' in str(typ):
            text = str(body).lower()
            if 'trigger' in text:
                for ms in self.markets.values():
                    if ms.group_id and ms.group_id in str(body):
                        self._pause(ms, 'order group triggered')
                        self._alert(ms, 'order_group', f'Order group triggered: {body}', level='error')
        elif typ == 'error':
            log.warning('ws error: %s', body)

    def _touch(self, ticker: str) -> None:
        c = self.contracts.get(ticker)
        if c is not None:
            self.dirty.add(c.market)

    # ── Fills and the fallback ───────────────────────────────────────────────

    def on_fill(self, ev: FillEvent) -> None:
        c = self.contracts.get(ev.ticker)
        ms = self._market_of(ev.ticker)
        self.positions[ev.ticker] += ev.count if ev.side == 'yes' else -ev.count
        if ms is None or c is None:
            return
        self.dirty.add(ms.spec.slug)
        try:
            store.record_fill(ms.spec.slug, ms.event_date, {
                'trade_id': ev.trade_id or f'{ev.coid}-{ev.count}-{self.wall()}', 'ticker': ev.ticker,
                'side': ev.side, 'tier': ev.tier, 'price_c': ev.price, 'count': ev.count,
                'username': c.username, 'client_order_id': ev.coid})
        except Exception:  # noqa: BLE001
            log.exception('could not record fill')
        if ev.tier != ANCHOR:
            return
        self._alert(ms, 'anchor_fill',
                    f'{c.subtitle} top-{c.n} anchor {ev.side.upper()} filled {ev.count:g} @ {ev.price}¢ '
                    f'| remaining {ev.remaining:g}/{ev.order_size:g} | pos {self.positions[ev.ticker]:g}',
                    username=c.username, ticker=ev.ticker,
                    data={'side': ev.side, 'price': ev.price, 'count': ev.count, 'remaining': ev.remaining})
        order = self.oms.closed.get(ev.coid) or self.oms.by_coid.get(ev.coid)
        if ev.fully_filled and ev.order_size >= ms.params.wipe_min_qty and order is not None \
                and not order.wipe_reported:
            order.wipe_reported = True
            self.apply_wipe(ms, c, ev.side)

    def apply_wipe(self, ms: MarketState, c: Contract, side: str) -> None:
        """An anchor was fully taken: cut the player's fair value by a ratio across
        every one of their markets, or pull the player after `max_wipes`."""
        p = ms.params
        key = self._player_key(c)
        ovr = ms.overrides.get(key) or {'status': 'active', 'yes_factor': 1.0, 'no_factor': 1.0,
                                        'wipes': 0, 'markup_override': None}
        wipes = int(ovr.get('wipes') or 0) + 1
        yes_f, no_f = float(ovr.get('yes_factor') or 1.0), float(ovr.get('no_factor') or 1.0)
        if side == 'yes':
            yes_f *= (1 - p.wipe_ratio)
        elif p.wipe_mirror:
            no_f *= (1 - p.wipe_ratio)
        status = 'pulled' if wipes >= p.max_wipes else 'active'
        ovr.update(status=status, yes_factor=yes_f, no_factor=no_f, wipes=wipes)
        ms.overrides[key] = ovr
        store.upsert_override(ms.spec.slug, ms.event_date, key, by='mm', player_name=c.subtitle,
                              status=status, yes_factor=yes_f, no_factor=no_f, wipes=wipes)
        if status == 'pulled':
            self._alert(ms, 'player_pulled',
                        f'{c.subtitle}: {wipes} anchor wipes — pulled from all markets. '
                        'Repost from the Market maker page.', level='warning',
                        username=key, ticker=c.ticker, data={'wipes': wipes, 'side': side})
        else:
            cut = round(p.wipe_ratio * 100)
            self._alert(ms, 'fallback',
                        f'{c.subtitle}: {side.upper()} anchor wiped on top-{c.n}; {side.upper()} fair value '
                        f'cut {cut}% across all of their markets (wipe {wipes}/{p.max_wipes})',
                        level='warning', username=key, ticker=c.ticker,
                        data={'side': side, 'yes_factor': yes_f, 'no_factor': no_f, 'wipes': wipes})
        self.dirty.add(ms.spec.slug)

    def _pause(self, ms: MarketState, reason: str) -> None:
        ms.paused, ms.pause_reason = True, reason
        store.set_paused(ms.spec.slug, True, reason, by='mm')
        self.dirty.add(ms.spec.slug)

    # ── Quoting ──────────────────────────────────────────────────────────────

    def _market_targets(self, ms: MarketState) -> dict[Slot, Quote | None]:
        targets: dict[Slot, Quote | None] = {}
        quotes: dict[str, dict] = {}
        active = (not ms.paused and not ms.held and ms.fv is not None
                  and self._quoting_window_open(ms))
        p = ms.params
        now = self.mono()
        for ticker in ms.tickers:
            c = self.contracts[ticker]
            book = self.books[ticker]
            ovr = ms.overrides.get(self._player_key(c), {})
            fair_yes = ms.fv.fair_yes(c.username, c.n) if (ms.fv and c.username) else None
            row = {'player': c.subtitle, 'username': c.username, 'n': c.n, 'fair_yes': fair_yes,
                   'status': ovr.get('status', 'active'), 'stale': book.stale}
            for side in SIDES:
                a_slot, s_slot = (ticker, side, ANCHOR), (ticker, side, SCOUT)
                targets[a_slot] = targets[s_slot] = None
                if not active or fair_yes is None or ovr.get('status') == 'pulled':
                    continue
                fair = side_fair(fair_yes, side, float(ovr.get('yes_factor') or 1.0),
                                 float(ovr.get('no_factor') or 1.0))
                war = self._war_until.get((ticker, side), 0) > now
                sq = quote_side(book, side, fair, self.oms.own_levels(ticker, side), p,
                                markup=ovr.get('markup_override'), war_mode=war, scouts=self.scouts)
                targets[a_slot], targets[s_slot] = sq.anchor, sq.scout
                row[side] = {'fair': round(fair, 4), 'note': sq.note, 'war': war,
                             'best_ex_own': book.best_bid_ex_own(side, self.oms.own_levels(ticker, side))}
            quotes[ticker] = row
        player_of = {t: self._player_key(self.contracts[t]) for t in ms.tickers}
        checked = risk.check(targets, self.positions, player_of, p)
        for ticker, row in quotes.items():
            for side in SIDES:
                if side in row:
                    row[side]['anchor'] = _q(checked.get((ticker, side, ANCHOR)))
                    row[side]['scout'] = _q(checked.get((ticker, side, SCOUT)))
        self.last_quotes.update(quotes)
        return checked

    def _apply_target(self, slot: Slot, q: Quote | None, p: MMParams) -> None:
        """Dwell and price-war filter for scouts; everything else applies at once."""
        now = self.mono()
        current = self.oms.targets.get(slot)
        if q == current:
            self._dwell.pop(slot, None)
            return
        ticker, side, tier = slot
        live = self.oms.orders.get(slot)
        immediate = (tier == ANCHOR or q is None or current is None
                     or (live is not None and q.price > live.price))        # outbid: move now
        if not immediate:
            pending = self._dwell.get(slot)
            if pending is None or pending[0] != q:
                self._dwell[slot] = (q, now)
                return
            if now - pending[1] < p.min_dwell_ms / 1000:
                return
        self._dwell.pop(slot, None)
        if tier == SCOUT and q is not None and current is not None and q.price != current.price:
            hist = self._reprices[(ticker, side)]
            hist.append(now)
            while hist and now - hist[0] > 60:
                hist.popleft()
            if len(hist) > p.war_max_reprices and self._war_until.get((ticker, side), 0) <= now:
                # Price war: rest at the buffer edge for the cool-off (no alert; too noisy).
                self._war_until[(ticker, side)] = now + p.war_cooloff_s
        self.oms.set_target(slot, q)

    def quote_tick(self) -> None:
        dirty, self.dirty = self.dirty, set()
        # Pending dwell decisions need re-checking even without new book data.
        dirty |= {self.contracts[s[0]].market for s in self._dwell if s[0] in self.contracts}
        for slug in dirty:
            ms = self.markets.get(slug)
            if ms is None:
                continue
            for slot, q in self._market_targets(ms).items():
                self._apply_target(slot, q, ms.params)

    # ── Execution ────────────────────────────────────────────────────────────

    async def exec_tick(self) -> None:
        actions = self.oms.next_actions()
        if not actions:
            return
        creates = [a for a in actions if a.kind == 'create']
        others = [a for a in actions if a.kind != 'create']
        if len(creates) >= 3:
            for i in range(0, len(creates), 20):
                self._spawn(self._run_batch(creates[i:i + 20]))
        else:
            others += creates
        for a in others:
            self._spawn(self._run_one(a))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def drain(self) -> None:
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    def _journal_result(self, a: Action, r: Any) -> None:
        o = a.order
        self.journal.write('action', {'kind': a.kind, 'coid': o.coid, 'ticker': o.ticker, 'side': o.side,
                                      'tier': o.tier, 'price': a.price, 'size': a.size, 'ok': r.ok,
                                      'status': r.status, 'error': r.error})
        if not r.ok:
            log.warning('%s %s %s %s failed: %s', a.kind, o.ticker, o.side, o.tier, r.error)
            if r.error and 'order_group' in r.error.lower():
                ms = self._market_of(o.ticker)
                if ms is not None and not ms.paused:
                    self._pause(ms, 'order group rejected orders')
                    self._alert(ms, 'order_group', f'Order group is blocking orders: {r.error}', level='error')

    async def _run_one(self, a: Action) -> None:
        o = a.order
        async with self._sem:
            r = await _to_thread(self.gateway.execute, a, self.oms.expiration_ts.get(o.ticker),
                                 self.oms.order_group.get(o.ticker))
        self.oms.on_result(a, r.ok, r.response, r.error, r.status)
        self._journal_result(a, r)
        self._touch(o.ticker)

    async def _run_batch(self, actions: list[Action]) -> None:
        async with self._sem:
            results = await _to_thread(self.gateway.batch_create, actions, self.oms.expiration_ts,
                                       self.oms.order_group)
        for a, r in zip(actions, results):
            self.oms.on_result(a, r.ok, r.response, r.error, r.status)
            self._journal_result(a, r)
            self._touch(a.order.ticker)

    # ── Slow path ────────────────────────────────────────────────────────────

    def _due(self, key: str, every: float) -> bool:
        now = self.mono()
        if now - self._last[key] >= every:
            self._last[key] = now
            return True
        return False

    def poll_control(self, ms: MarketState) -> None:
        ctl = store.get_control(ms.spec.slug)
        try:
            params = store.params_for(ms.spec.slug, ctl)
        except Exception as e:  # noqa: BLE001
            log.warning('[%s] bad params ignored: %s', ms.spec.slug, e)
            params = ms.params
        if ctl['paused'] != ms.paused or params != ms.params:
            self.dirty.add(ms.spec.slug)
        ms.paused, ms.pause_reason, ms.params = ctl['paused'], ctl['reason'], params
        if ms.event_date:
            overrides = store.get_overrides(ms.spec.slug, ms.event_date)
            if _ovr_key(overrides) != _ovr_key(ms.overrides):
                self.dirty.add(ms.spec.slug)
            ms.overrides = overrides

    def market_order_count(self, ms: MarketState) -> int:
        return sum(1 for o in list(self.oms.orders.values()) if o.ticker in ms.tickers)

    def run_cutoff(self, ms: MarketState) -> None:
        """At the cutoff: empty targets, cancel every MM order on the event, verify."""
        if ms.cutoff_done or ms.event_date is None or self.wall() < ms.t_cut:
            return
        ms.cutoff_done = True
        self.oms.clear_targets(ms.tickers)
        remaining = 0
        if self.live:
            pattern = ms.spec.ticker_pattern(_to_kalshi_date(ms.event_date))
            ours = [o for o in self.client.get_open_orders()
                    if is_mm_order(o) and pattern.search(o.get('ticker', ''))]
            if ours:
                self.client.batch_cancel_orders([(o['order_id'], o.get('ticker')) for o in ours])
                left = [o for o in self.client.get_open_orders()
                        if is_mm_order(o) and pattern.search(o.get('ticker', ''))]
                remaining = len(left)
        level = 'error' if remaining else 'info'
        self._alert(ms, 'cutoff', f'Cutoff reached for {ms.event_date}: '
                    + (f'{remaining} order(s) STILL RESTING — check Kalshi' if remaining
                       else 'all market-maker orders gone'), level=level)

    def reconcile(self, orders: list[dict]) -> list[dict]:
        """Apply REST open orders to the OMS (loop thread). Returns orders to cancel."""
        tickers = set(self.all_tickers())
        orphans = self.oms.reconcile(orders, lambda o: is_mm_order(o) and o.get('ticker') in tickers)
        stray = [o for o in orders if is_mm_order(o) and o.get('ticker') not in tickers]
        to_cancel = orphans + [o for o in stray if self._is_registered_ticker(o.get('ticker', ''))]
        if to_cancel:
            log.warning('cancelling %d orphaned market-maker orders', len(to_cancel))
        return to_cancel

    def _is_registered_ticker(self, ticker: str) -> bool:
        return any(ticker.startswith((ms.spec.winner_series + '-', ms.spec.top_series + '-'))
                   for ms in self.markets.values())

    def refresh_positions(self) -> None:
        df = self.client.get_positions()
        if df is None or df.empty or 'ticker' not in df.columns:
            return
        col = 'position_fp' if 'position_fp' in df.columns else 'position'
        tickers = set(self.all_tickers())
        for _, r in df.iterrows():
            if r['ticker'] in tickers:
                self.positions[r['ticker']] = float(r[col])

    def status_payload(self, ms: MarketState) -> dict:
        orders = [o for o in list(self.oms.orders.values()) if o.ticker in ms.tickers]
        quotes = {t: {**self.last_quotes.get(t, {}), 'position': self.positions.get(t, 0.0),
                      'live': [{'side': o.side, 'tier': o.tier, 'price': o.price,
                                'remaining': o.remaining, 'state': o.state}
                               for o in orders if o.ticker == t]}
                  for t in sorted(ms.tickers)}
        return {
            'live': self.live, 'scouts': self.scouts and ms.params.scouts_enabled,
            'event_date': ms.event_date,
            't_cut': datetime.fromtimestamp(ms.t_cut, timezone.utc).isoformat() if ms.t_cut else None,
            'window_open': self._quoting_window_open(ms), 'cutoff_done': ms.cutoff_done,
            'paused': ms.paused, 'pause_reason': ms.pause_reason, 'held': ms.held,
            'fv_version': ms.fv.version if ms.fv else None, 'fv_errors': ms.fv_errors,
            'ws_connected': self.ws_connected,
            'stale_books': sum(1 for t in ms.tickers if self.books[t].stale),
            'markets': len(ms.tickers), 'unmatched': ms.unmatched, 'skipped': ms.skipped[:50],
            'orders': {'anchor': sum(1 for o in orders if o.tier == ANCHOR),
                       'scout': sum(1 for o in orders if o.tier == SCOUT)},
            'resting_notional': round(sum(o.price * o.remaining / 100 for o in orders), 2),
            'tokens': round(self.oms.bucket.available(), 1), 'oms': dict(self.oms.stats),
            'params': ms.params.model_dump(),
            'quotes': quotes,
        }

    def slow_tick(self) -> None:
        for ms in self.markets.values():
            try:
                if self._due(f'event:{ms.spec.slug}', 60):
                    self.refresh_event(ms)
                self.poll_control(ms)
                if self._due(f'fv:{ms.spec.slug}', 30) and self.reload_fair_values(ms):
                    self.discover(ms)         # names may map differently under a new run
                    self._last[f'disc:{ms.spec.slug}'] = self.mono()
                if self._due(f'disc:{ms.spec.slug}', 300):
                    self.discover(ms)
                self.run_cutoff(ms)
                if self._due(f'status:{ms.spec.slug}', 3):
                    paused_ack = ms.paused and self.market_order_count(ms) == 0
                    store.write_status(ms.spec.slug, 'live' if self.live else 'shadow', paused_ack,
                                       self.status_payload(ms))
            except Exception:  # noqa: BLE001 — one market's DB/API blip must not stop the others
                log.exception('[%s] slow tick failed', ms.spec.slug)
        try:
            if self._due('positions', 60) and self.live:
                self.refresh_positions()
        except Exception:  # noqa: BLE001
            log.exception('positions refresh failed')
        self.journal.flush()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def startup(self) -> None:
        """Cancel leftover market-maker orders, create order groups, load state."""
        from .. import job_queue
        job_queue.init_schema()                  # queue tables + mm_* tables
        for ms in self.markets.values():
            try:
                self.refresh_event(ms)
                self.poll_control(ms)
                self.reload_fair_values(ms)
                self.discover(ms)
            except Exception:  # noqa: BLE001 — the slow tick retries; other markets carry on
                log.exception('[%s] startup load failed', ms.spec.slug)
            self._last[f'event:{ms.spec.slug}'] = self._last[f'fv:{ms.spec.slug}'] = \
                self._last[f'disc:{ms.spec.slug}'] = self.mono()
        if not self.live:
            return
        orders = self.client.get_open_orders()
        leftovers = [o for o in orders if is_mm_order(o) and self._is_registered_ticker(o.get('ticker', ''))]
        if leftovers:
            log.info('startup: cancelling %d leftover market-maker orders', len(leftovers))
            self.client.batch_cancel_orders([(o['order_id'], o.get('ticker')) for o in leftovers])
        self.oms.reconcile([o for o in orders if not is_mm_order(o)], is_mm_order)   # seed external orders
        for ms in self.markets.values():
            self.ensure_order_group(ms)
            for t in ms.tickers:
                self.oms.order_group[t] = ms.group_id
        self.refresh_positions()

    def ensure_order_group(self, ms: MarketState) -> None:
        """One exchange-side circuit breaker per market and event (live only)."""
        if not self.live or ms.group_id is not None or ms.event_date is None:
            return
        try:
            ms.group_id = self.client.create_order_group(ms.params.order_group_limit)
        except Exception as e:  # noqa: BLE001
            self._alert(ms, 'order_group', f'Could not create order group ({e}); quoting without it',
                        level='warning')

    async def _loop(self, fn: Callable[[], Any], every: float) -> None:
        while not self._stop.is_set():
            try:
                r = fn()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                log.exception('%s failed', getattr(fn, '__name__', fn))
            await asyncio.sleep(every)

    async def reconcile_tick(self) -> None:
        """REST calls in a thread; OMS changes on the loop thread, like every other OMS change."""
        if not self.live:
            return
        orders = await _to_thread(self.client.get_open_orders)
        to_cancel = self.reconcile(orders)
        if to_cancel:
            await _to_thread(self.client.batch_cancel_orders,
                             [(o['order_id'], o.get('ticker')) for o in to_cancel])

    async def _slow_loop(self) -> None:
        # slow_tick runs in a thread so REST and DB latency never stalls quoting.
        # It only reassigns MarketState fields and clears targets; the OMS's
        # order book of record is changed on the loop thread.
        while not self._stop.is_set():
            try:
                await _to_thread(self.slow_tick)
                if self._due('reconcile', 60):
                    await self.reconcile_tick()
                await self._sync_subscription()
            except Exception:  # noqa: BLE001
                log.exception('slow loop failed')
            await asyncio.sleep(1.0)

    async def run(self) -> None:
        await _to_thread(self.startup)
        tasks = [asyncio.create_task(self.ws_loop()),
                 asyncio.create_task(self._loop(self.quote_tick, 0.05)),
                 asyncio.create_task(self._loop(self.exec_tick, 0.02)),
                 asyncio.create_task(self._slow_loop())]
        await self._stop.wait()
        log.info('shutting down: cancelling market-maker orders')
        for t in tasks:
            t.cancel()
        await self.shutdown()

    async def shutdown(self) -> None:
        await self.drain()
        self.oms.clear_targets()
        for _ in range(20):
            await self.exec_tick()
            await self.drain()
            if not self.oms.orders:
                break
            await asyncio.sleep(0.25)
        if self.live:
            try:
                await _to_thread(self._cancel_all_mm)
            except Exception:  # noqa: BLE001
                log.exception('final cancel-all failed')
        self.journal.close()

    def _cancel_all_mm(self) -> None:
        ours = [o for o in self.client.get_open_orders()
                if is_mm_order(o) and self._is_registered_ticker(o.get('ticker', ''))]
        if ours:
            self.client.batch_cancel_orders([(o['order_id'], o.get('ticker')) for o in ours])


def _ovr_key(overrides: dict[str, dict]) -> dict:
    return {k: (v.get('status'), v.get('yes_factor'), v.get('no_factor'), v.get('markup_override'))
            for k, v in overrides.items()}


def _q(q: Quote | None) -> dict | None:
    return None if q is None else {'price': q.price, 'size': q.size}


def from_env_flags() -> tuple[bool, bool]:
    """(live, scouts) from MM_LIVE / MM_SCOUTS."""
    live = os.environ.get('MM_LIVE', '0') == '1'
    scouts = os.environ.get('MM_SCOUTS', '1') == '1'
    return live, scouts
