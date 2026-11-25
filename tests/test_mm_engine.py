"""Market-maker engine end to end against SQLite and a fake Kalshi client.

Drives the engine's ticks directly (no WebSocket, no network): startup →
book snapshots → quote → execute → fills → wipes → pull → repost → pause →
cutoff, plus the tt-worker watchdog and the kill switch's pause.
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone

import pandas as pd
import pytest

import src.data
import src.pipeline
from src import db, job_queue as q, strategies
from src.markets import TITLED_TUESDAY
from src.mm import fair_value, store, watchdog
from src.mm.config import MMParams
from src.mm.engine import Engine
from src.mm.gateway import LiveGateway, ShadowGateway
from src.mm.oms import ANCHOR, EXIT, SCOUT

DATE, PREV, KDATE, MARKET = '2026-11-17', '2026-11-10', '26NOV17', 'titled_tuesday'
WIN_A = f'KXTITLEDTUESDAY-{KDATE}-AAA'
WIN_B = f'KXTITLEDTUESDAY-{KDATE}-BBB'
TOP3_A = f'KXTITLEDTUESTOP-{KDATE}T3-AAA'
BEFORE_CUTOFF = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc).timestamp()
AFTER_CUTOFF = datetime(2026, 11, 17, 14, 30, tzinfo=timezone.utc).timestamp()   # 09:30 ET


def _preds() -> pd.DataFrame:
    # Ten players whose probabilities sum correctly: P(win)=0.1 each, P(top3)=0.3, ...
    users = ['a', 'b'] + [f'p{i}' for i in range(8)]
    df = pd.DataFrame({'username': users, 'market': MARKET, 'p_participate': 1.0,
                       'P_top1_given_play': 0.1, 'P_top3_given_play': 0.3,
                       'P_top5_given_play': 0.5, 'P_top8_given_play': 0.8})
    df.loc[0, ['P_top1_given_play', 'P_top3_given_play']] = [0.30, 0.50]   # 'a' is the favourite
    df.loc[1, ['P_top1_given_play', 'P_top3_given_play']] = [0.0, 0.1]   # 'b' never wins
    return df


class FakeKalshi:
    def __init__(self):
        self.open_orders: list[dict] = []
        self.cancelled: list[str] = []
        self.created: list[dict] = []

    def get_markets_by_event(self, event_ticker):
        names = {'AAA': 'Player A', 'BBB': 'Player B'}
        if event_ticker == f'KXTITLEDTUESDAY-{KDATE}':
            return [{'ticker': f'{event_ticker}-{c}', 'yes_sub_title': n, 'status': 'active'}
                    for c, n in names.items()]
        if event_ticker == f'KXTITLEDTUESTOP-{KDATE}T3':
            return [{'ticker': TOP3_A, 'yes_sub_title': 'Player A', 'status': 'active'}]
        return []

    def get_open_orders(self):
        return list(self.open_orders)

    def batch_cancel_orders(self, pairs):
        ids = {oid for oid, _ in pairs}
        self.cancelled += sorted(ids)
        self.open_orders = [o for o in self.open_orders if o['order_id'] not in ids]
        return [{'order_id': i, 'reduced_by': '1.00'} for i in ids]

    def get_positions(self):
        return pd.DataFrame()

    def create_order_group(self, limit):
        return 'og-1'

    order_body = staticmethod(__import__('kalshi_core').KalshiClient.order_body)

    def create_order(self, **kw):
        self.created.append(kw)
        oid = f'o{len(self.created)}'
        self.open_orders.append({'order_id': oid, 'client_order_id': kw['client_order_id'],
                                 'ticker': kw['ticker']})
        return {'order_id': oid, 'remaining_count': f"{kw['count']:.2f}"}

    def batch_create_orders(self, bodies):
        out = []
        for b in bodies:
            self.created.append(b)
            oid = f'o{len(self.created)}'
            self.open_orders.append({'order_id': oid, 'client_order_id': b['client_order_id'],
                                     'ticker': b['ticker']})
            out.append({'order_id': oid, 'client_order_id': b['client_order_id'],
                        'remaining_count': b['count']})
        return out

    def amend_order(self, *a, **k):
        return {'remaining_count': None}

    def decrease_order(self, order_id, reduce_to, market_ticker=None):
        return {'remaining_count': f'{reduce_to:.2f}'}

    def cancel_order(self, order_id, market_ticker=None):
        self.cancelled.append(order_id)
        self.open_orders = [o for o in self.open_orders if o['order_id'] != order_id]
        return {'order_id': order_id}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'mm.db')
    q.init_schema()
    with db.get_conn() as conn:
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER, market TEXT, tourn_date TEXT, state TEXT, "
                     "completed_at TEXT)")
        conn.execute("INSERT INTO pipeline_runs VALUES (1, ?, ?, 'completed', '2026-11-12T10:00:00')",
                     (MARKET, PREV))
    preds = _preds()
    monkeypatch.setattr(fair_value, 'read_market_rows', lambda table, market: preds.copy())
    monkeypatch.setattr(src.pipeline, 'next_tourn_date', lambda spec=None: DATE)
    monkeypatch.setattr(src.data, 'market_player_to_username',
                        lambda usernames, spec=None: {'Player A': 'a', 'Player B': 'b'})
    return preds


def make_engine(client=None, live=False, wall=BEFORE_CUTOFF, **kw):
    client = client or FakeKalshi()
    clock = {'t': wall}
    gw = LiveGateway(client) if live else ShadowGateway()
    eng = Engine([TITLED_TUESDAY], client, gw, wall=lambda: clock['t'], write_budget=(1e6, 1e6), **kw)
    eng.startup()
    return eng, client, gw, clock


def snap(ticker, yes=(), no=()):
    return {'type': 'orderbook_snapshot', 'sid': 1, 'seq': 1, 'msg': {
        'market_ticker': ticker,
        'yes_dollars_fp': [[f'{p / 100:.4f}', f'{s}.00'] for p, s in yes],
        'no_dollars_fp': [[f'{p / 100:.4f}', f'{s}.00'] for p, s in no]}}


def cycle(eng, n=3):
    for _ in range(n):
        eng.quote_tick()
        asyncio.run(_exec(eng))


async def _exec(eng):
    await eng.exec_tick()
    await eng.drain()


def live_slots(eng):
    return {(o.ticker, o.side, o.tier): (o.price, int(o.remaining)) for o in eng.oms.orders.values()}


def test_fair_value_validation():
    df = _preds().set_index('username')
    assert fair_value.validate(df, (1, 3, 5, 8)) == []
    bad = df.copy()
    bad.loc['a', 'P_top3_given_play'] = 0.2                # below P(win)
    assert any('P_top1_given_play > P_top3_given_play' in e for e in fair_value.validate(bad, (1, 3)))
    assert fair_value.validate(df.iloc[:2], (1,))           # sums nowhere near 1


def test_shadow_quotes_anchors_and_scouts(env):
    eng, client, gw, _ = make_engine()
    assert eng.markets[MARKET].fv is not None and eng.markets[MARKET].tickers == {WIN_A, WIN_B, TOP3_A}
    eng.handle(snap(WIN_A, yes=[(22, 50)], no=[(40, 10)]))
    for t in (WIN_B, TOP3_A):
        eng.handle(snap(t))
    cycle(eng)
    slots = live_slots(eng)
    # Player A wins with fair 0.30: anchor 20¢, scout one above the 22¢ competitor.
    assert slots[(WIN_A, 'yes', SCOUT)] == (23, 140) and slots[(WIN_A, 'yes', ANCHOR)] == (20, 60)
    assert slots[(WIN_A, 'no', ANCHOR)][0] == 55
    assert (WIN_B, 'yes', ANCHOR) not in slots                 # P(win) 0 → no YES bid
    assert not client.created                                 # shadow sends nothing
    st = store.get_status(MARKET)
    assert st is None or st['mode'] == 'shadow'


def test_live_orders_carry_expiry_group_and_prefix(env):
    eng, client, _, _ = make_engine(live=True)
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    cycle(eng)
    t_cut = TITLED_TUESDAY.start_ts(DATE) - 120 * 60
    assert client.created and all(b['expiration_time'] == t_cut for b in client.created)
    assert all(b['order_group_id'] == 'og-1' and b['post_only'] for b in client.created)
    assert all(b['client_order_id'].startswith(('mma-', 'mms-', 'mmx-')) for b in client.created)


def test_startup_cancels_only_market_maker_leftovers(env):
    client = FakeKalshi()
    client.open_orders = [
        {'order_id': 'old-mm', 'client_order_id': 'mma-x', 'ticker': WIN_A},
        {'order_id': 'fade', 'client_order_id': None, 'ticker': WIN_A, 'outcome_side': 'no',
         'action': 'buy', 'no_price_dollars': '0.6000', 'remaining_count': 10},
    ]
    eng, client, _, _ = make_engine(client, live=True)
    assert client.cancelled == ['old-mm']
    assert eng.oms.own_levels(WIN_A, 'no') == {60: 10.0}       # the fade bid is "ours" in the book


def _wipe(eng, side='yes', ticker=WIN_A):
    o = eng.oms.orders[(ticker, side, ANCHOR)]
    eng.handle({'type': 'fill', 'sid': 9, 'msg': {
        'trade_id': f'tr-{o.coid}', 'client_order_id': o.coid, 'order_id': o.order_id,
        'market_ticker': ticker, 'count_fp': f'{o.remaining:.2f}',
        'yes_price_dollars': f'{(o.price if side == "yes" else 100 - o.price) / 100:.4f}'}})


def test_wipes_cut_the_whole_player_then_pull_and_repost(env):
    eng, _, _, _ = make_engine()
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    cycle(eng)
    top3_before = live_slots(eng)[(TOP3_A, 'yes', ANCHOR)][0]

    _wipe(eng, 'yes', WIN_A)
    ovr = store.get_overrides(MARKET, DATE)['a']
    assert ovr['wipes'] == 1 and ovr['yes_factor'] == pytest.approx(0.67) and ovr['status'] == 'active'
    eng.poll_control(eng.markets[MARKET])
    cycle(eng)
    # The cut applies across the player's ladder, as a ratio: top-3 fair 0.50 → 0.335.
    assert live_slots(eng)[(TOP3_A, 'yes', ANCHOR)][0] < top3_before
    assert live_slots(eng)[(WIN_A, 'yes', ANCHOR)][0] == 13      # fair 0.201 / 1.5

    _wipe(eng, 'yes', TOP3_A)
    assert store.get_overrides(MARKET, DATE)['a']['status'] == 'pulled'
    eng.poll_control(eng.markets[MARKET])
    cycle(eng)
    assert not any(s[0] in (WIN_A, TOP3_A) for s in live_slots(eng))
    kinds = [a['kind'] for a in store.list_alerts(MARKET)]
    assert 'player_pulled' in kinds and 'fallback' in kinds and kinds.count('anchor_fill') == 2

    store.repost_player(MARKET, DATE, 'a', markup=2.0)
    eng.poll_control(eng.markets[MARKET])
    cycle(eng)
    assert live_slots(eng)[(WIN_A, 'yes', ANCHOR)][0] == 15      # max(0.30/2, 0.30 − 0.15)


def test_small_fill_is_never_a_wipe(env):
    eng, _, _, _ = make_engine()
    store.set_params(MARKET, {'wipe_min_qty': 10, 'anchor_volume': 5})
    eng.poll_control(eng.markets[MARKET])
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    cycle(eng)
    _wipe(eng, 'yes', WIN_A)
    assert 'a' not in store.get_overrides(MARKET, DATE)


def test_exits_are_off_by_default(env):
    eng, _, _, _ = make_engine()
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    eng.positions[WIN_A] = 50
    eng.positions[TOP3_A] = -20
    cycle(eng)
    assert not any(s[2] == EXIT for s in live_slots(eng))


def test_exits_rest_held_positions_at_fair_and_track_fills(env):
    eng, _, _, _ = make_engine()
    store.set_params(MARKET, {'exits_enabled': True})
    eng.poll_control(eng.markets[MARKET])
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    eng.positions[WIN_A] = 50          # long YES on Player A to win (fair 0.30)
    eng.positions[TOP3_A] = -20        # long NO on Player A top 3 (fair 0.50)
    cycle(eng)
    slots = live_slots(eng)
    assert slots[(WIN_A, 'no', EXIT)] == (70, 50)          # sell YES at 30¢
    assert slots[(TOP3_A, 'yes', EXIT)] == (50, 20)        # sell NO at 50¢
    assert (WIN_A, 'yes', EXIT) not in slots and (WIN_B, 'no', EXIT) not in slots
    assert slots[(WIN_A, 'no', ANCHOR)][0] == 55            # the NO anchor still quotes
    # A partial exit fill shrinks the order to the remaining position.
    o = eng.oms.orders[(WIN_A, 'no', EXIT)]
    eng.handle({'type': 'fill', 'msg': {'trade_id': 't1', 'order_id': o.order_id,
                                        'client_order_id': o.coid, 'count_fp': '30.00',
                                        'yes_price_dollars': '0.3000'}})
    assert eng.positions[WIN_A] == 20
    cycle(eng)
    assert live_slots(eng)[(WIN_A, 'no', EXIT)] == (70, 20)
    # Switched off from the dashboard, the exits come down.
    eng.markets[MARKET].params = MMParams(exits_enabled=False)
    eng.dirty.add(MARKET)
    cycle(eng)
    assert not any(s[2] == EXIT for s in live_slots(eng))


def test_pause_clears_everything_and_acks(env):
    eng, _, _, _ = make_engine()
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    cycle(eng)
    assert eng.oms.orders
    store.set_paused(MARKET, True, 'test')
    eng.poll_control(eng.markets[MARKET])
    cycle(eng)
    assert not eng.oms.orders
    eng.slow_tick()
    assert store.get_status(MARKET)['paused_ack'] is True


def test_cutoff_stops_quoting_and_cancels(env):
    eng, client, _, clock = make_engine(live=True)
    for t in (WIN_A, WIN_B, TOP3_A):
        eng.handle(snap(t))
    cycle(eng)
    client.open_orders.append({'order_id': 'manual', 'client_order_id': None, 'ticker': WIN_A})
    clock['t'] = AFTER_CUTOFF
    eng.run_cutoff(eng.markets[MARKET])
    cycle(eng)
    assert not eng.oms.orders
    assert [o['order_id'] for o in client.open_orders] == ['manual']   # manual order untouched
    assert store.list_alerts(MARKET)[0]['kind'] == 'cutoff'


def test_seq_gap_marks_books_stale_and_drops_scouts(env):
    eng, _, _, _ = make_engine()
    eng.handle(snap(WIN_A, yes=[(22, 50)]))
    eng.handle({'type': 'orderbook_delta', 'sid': 1, 'seq': 5, 'msg': {
        'market_ticker': WIN_A, 'side': 'yes', 'price_dollars': '0.2100', 'delta_fp': '5.00'}})
    assert eng.books[WIN_A].stale
    cycle(eng)
    assert (WIN_A, 'yes', SCOUT) not in live_slots(eng) and (WIN_A, 'yes', ANCHOR) in live_slots(eng)


# ── Watchdog and kill switch (tt-worker side) ────────────────────────────────

def test_watchdog_cancels_mm_orders_after_cutoff_only(env, monkeypatch):
    client = FakeKalshi()
    client.open_orders = [{'order_id': 'mm', 'client_order_id': 'mms-1', 'ticker': WIN_A},
                          {'order_id': 'fade', 'client_order_id': None, 'ticker': WIN_A}]
    m = dataclasses.replace(strategies.MARKETS[MARKET], next_event_date=lambda: DATE)
    markets = {MARKET: m}
    watchdog.check(client, markets, now=BEFORE_CUTOFF)      # only last week's event is past its cutoff
    assert client.cancelled == []
    watchdog.check(client, markets, now=AFTER_CUTOFF)
    assert client.cancelled == ['mm']


def test_watchdog_pulls_scouts_when_heartbeat_stale(env, monkeypatch):
    client = FakeKalshi()
    client.open_orders = [{'order_id': 's', 'client_order_id': 'mms-1', 'ticker': WIN_A},
                          {'order_id': 'x', 'client_order_id': 'mmx-1', 'ticker': WIN_A},
                          {'order_id': 'a', 'client_order_id': 'mma-1', 'ticker': WIN_A}]
    m = dataclasses.replace(strategies.MARKETS[MARKET], next_event_date=lambda: DATE)
    store.write_status(MARKET, 'live', False, {})
    with db.get_conn() as conn:
        conn.execute("UPDATE mm_status SET heartbeat_at = '2026-01-01T00:00:00.000+00:00'")
    watchdog.check(client, {MARKET: m}, now=BEFORE_CUTOFF)
    assert client.cancelled == ['s', 'x']


def test_kill_switch_pauses_market_maker_and_live_mm_blocks_place_bids(env):
    q.enqueue_kill(MARKET, DATE)
    assert store.get_control(MARKET)['paused'] is True
    store.set_paused(MARKET, False)
    store.write_status(MARKET, 'live', False, {'paused': False})
    with pytest.raises(strategies.SubmissionError, match='market maker is live'):
        strategies.submit(market=MARKET, strategy='place_bids', tourn_date=DATE, params={},
                          max_exposure=5)


def test_params_validate_and_round_trip(env):
    store.set_params(MARKET, {'buffer_c': 3, 'wipe_ratio': 0.5})
    p = store.params_for(MARKET)
    assert p.buffer_c == 3 and p.wipe_ratio == 0.5 and p.anchor_volume == MMParams().anchor_volume
    with pytest.raises(ValueError):
        store.set_params(MARKET, {'buffer_c': 0})
