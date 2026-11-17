"""Market maker: book, quoting, risk and order-manager units. No DB, no network."""
from __future__ import annotations

import pytest

from src.mm import risk
from src.mm.book import Book
from src.mm.config import MMParams, is_mm_order
from src.mm.gateway import LiveGateway, book_side, yes_price
from src.mm.oms import ANCHOR, OMS, RESTING, SCOUT, UNKNOWN, TokenBucket
from src.mm.quoting import Quote, quote_side, scout_volume, side_fair

T = 'KXTITLEDTUESDAY-26NOV17-AAA'
P = MMParams()          # markup 1.5, max_discount 15, AV 200, buffer 1


def book(yes=(), no=()):
    b = Book(T)
    b.apply_snapshot({'yes_dollars_fp': [[f'{p / 100:.4f}', str(q)] for p, q in yes],
                      'no_dollars_fp': [[f'{p / 100:.4f}', str(q)] for p, q in no]})
    return b


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ── Book ─────────────────────────────────────────────────────────────────────

def test_book_snapshot_delta_and_ex_own():
    b = book(yes=[(20, 100), (25, 10)], no=[(60, 5)])
    assert b.best_bid('yes') == 25 and b.cross_price('yes') == 40 and b.cross_price('no') == 75
    b.apply_delta({'side': 'yes', 'price_dollars': '0.2500', 'delta_fp': '-10.00'})
    assert b.best_bid('yes') == 20
    b.apply_delta({'side': 'yes', 'price_dollars': '0.2700', 'delta_fp': '50.00'})
    assert b.best_bid_ex_own('yes', {27: 50}) == 20       # our own 27¢ is not a competitor
    assert b.best_bid_ex_own('yes', {27: 30}) == 27       # someone else also rests at 27¢


# ── Quoting ──────────────────────────────────────────────────────────────────

def test_scout_improves_one_tick_and_sizes_linearly():
    # fair 0.30 → anchor max(20, 15) = 20¢; competitor at 22¢ → scout 23¢.
    sq = quote_side(book(yes=[(22, 50), (10, 5)]), 'yes', 0.30, {}, P)
    assert sq.scout == Quote(23, 140)                      # 200 × (30 − 23) / (30 − 20)
    assert sq.anchor == Quote(20, 60)                      # scout + anchor = AV


def test_scout_rests_at_buffer_edge_when_competitor_is_inside():
    sq = quote_side(book(yes=[(29, 50)]), 'yes', 0.30, {}, P)
    assert sq.scout == Quote(29, 20) and 'inside buffer' in sq.note


def test_no_scout_when_anchor_already_best_or_book_empty():
    assert quote_side(book(yes=[(15, 50)]), 'yes', 0.30, {}, P).scout is None
    sq = quote_side(book(), 'yes', 0.30, {}, P)
    assert sq.scout is None and sq.anchor == Quote(20, 200)


def test_scout_never_counts_own_order_as_competitor():
    # Our scout rests at 23¢; the best *other* bid is 21¢ → scout steps down to 22¢.
    sq = quote_side(book(yes=[(23, 140), (21, 10)]), 'yes', 0.30, {23: 140}, P)
    assert sq.scout.price == 22


def test_never_crosses_the_book():
    # A NO bid at 75¢ means YES can be bought at 25¢: the scout must stay below it.
    sq = quote_side(book(yes=[(24, 5)], no=[(75, 5)]), 'yes', 0.30, {}, P)
    assert sq.scout.price == 24
    # Anchor pulls back too if the opposite side has come down to it.
    sq = quote_side(book(no=[(81, 5)]), 'yes', 0.30, {}, P)
    assert sq.anchor.price == 18


def test_no_side_is_symmetric():
    # fair YES 0.30 → fair NO 0.70 → anchor max(46.7, 55) = 55¢.
    sq = quote_side(book(no=[(60, 10)]), 'no', side_fair(0.30, 'no'), {}, P)
    assert sq.anchor.price == 55 and sq.scout.price == 61


def test_war_mode_rests_at_edge_and_stale_book_drops_scouts():
    assert quote_side(book(yes=[(22, 5)]), 'yes', 0.30, {}, P, war_mode=True).scout.price == 29
    b = book(yes=[(22, 5)])
    b.stale = True
    sq = quote_side(b, 'yes', 0.30, {}, P)
    assert sq.scout is None and sq.anchor == Quote(20, 200)


def test_scouts_disabled_and_reposted_markup():
    assert quote_side(book(yes=[(22, 5)]), 'yes', 0.30, {}, P, scouts=False).scout is None
    assert quote_side(book(), 'yes', 0.30, {}, P, markup=1.2).anchor.price == 25


def test_side_fair_applies_player_factors():
    assert side_fair(0.30, 'yes', yes_factor=0.67) == pytest.approx(0.201)
    assert side_fair(0.30, 'no', no_factor=0.67) == pytest.approx(0.469)
    assert scout_volume(30.0, 29, 20, P) == 20 and scout_volume(30.0, 21, 20, P) == 180


# ── Risk ─────────────────────────────────────────────────────────────────────

def test_risk_position_trims_scout_before_anchor():
    p = MMParams(max_position=100)
    targets = {(T, 'yes', ANCHOR): Quote(20, 60), (T, 'yes', SCOUT): Quote(23, 140),
               (T, 'no', ANCHOR): Quote(55, 200), (T, 'no', SCOUT): None}
    out = risk.check(targets, {T: 50.0}, {T: 'a'}, p)
    assert out[(T, 'yes', ANCHOR)] == Quote(20, 50) and out[(T, 'yes', SCOUT)] is None
    assert out[(T, 'no', ANCHOR)] == Quote(55, 150)         # long YES 50 → NO room 150


def test_risk_player_notional_scales_ladder():
    p = MMParams(max_player_notional=50)
    t2 = T.replace('AAA', 'AAB')
    targets = {(T, 'yes', ANCHOR): Quote(50, 100), (t2, 'yes', ANCHOR): Quote(50, 100)}
    out = risk.check(targets, {}, {T: 'a', t2: 'a'}, p)
    assert sum(q.price * q.size / 100 for q in out.values()) <= 50


# ── OMS ──────────────────────────────────────────────────────────────────────

def oms(cap=1000.0, refill=1000.0):
    clock = Clock()
    return OMS(TokenBucket(cap, refill, clock=clock), clock=clock, cancel_reserve=4), clock


def ack(o, a, **resp):
    o.on_result(a, True, {'order_id': f'id-{a.order.coid}', 'remaining_count': f'{a.size}', **resp})


def test_oms_create_amend_decrease_cancel():
    m, _ = oms()
    slot = (T, 'yes', SCOUT)
    m.set_target(slot, Quote(23, 140))
    [a] = m.next_actions()
    assert a.kind == 'create' and a.order.coid.startswith('mms-')
    assert m.next_actions() == []                         # one request in flight per slot
    ack(m, a)
    m.set_target(slot, Quote(24, 120))
    [a] = m.next_actions()
    assert a.kind == 'amend' and (a.price, a.size) == (24, 120)
    m.on_result(a, True, {'remaining_count': '120'})
    m.set_target(slot, Quote(24, 100))
    [a] = m.next_actions()
    assert a.kind == 'decrease'
    m.on_result(a, True, {'remaining_count': '100'})
    m.set_target(slot, None)
    [a] = m.next_actions()
    assert a.kind == 'cancel' and a.priority == 0
    m.on_result(a, True, {})
    assert slot not in m.orders


def test_oms_partly_filled_order_is_cancelled_then_recreated():
    m, _ = oms()
    slot = (T, 'yes', ANCHOR)
    m.set_target(slot, Quote(20, 200))
    [a] = m.next_actions()
    ack(m, a)
    ev = m.on_fill({'trade_id': 't1', 'client_order_id': a.order.coid, 'count_fp': '50',
                    'yes_price_dollars': '0.2000'})
    assert ev.tier == ANCHOR and ev.remaining == 150 and not ev.fully_filled
    assert m.on_fill({'trade_id': 't1', 'client_order_id': a.order.coid, 'count_fp': '50'}) is None
    m.set_target(slot, Quote(19, 200))
    [c] = m.next_actions()
    assert c.kind == 'cancel'                              # amend total would be ambiguous after fills
    m.on_result(c, True, {})
    [n] = m.next_actions()
    assert n.kind == 'create' and n.price == 19


def test_oms_full_fill_reports_wipe_and_late_fill_after_close():
    m, _ = oms()
    slot = (T, 'no', ANCHOR)
    m.set_target(slot, Quote(55, 200))
    [a] = m.next_actions()
    ack(m, a)
    m.on_user_order({'order_id': f'id-{a.order.coid}', 'status': 'executed', 'remaining_count_fp': '0.00',
                     'last_updated_ts_ms': 5})
    ev = m.on_fill({'trade_id': 't9', 'order_id': f'id-{a.order.coid}', 'count_fp': '200',
                    'client_order_id': a.order.coid, 'yes_price_dollars': '0.4500'})
    assert ev.fully_filled and ev.price == 55 and ev.order_size == 200


def test_token_bucket_throttles_and_keeps_reserve_for_cancels():
    m, clock = oms(cap=12, refill=10)
    for i in range(3):
        m.set_target((f'{T}{i}', 'yes', ANCHOR), Quote(20, 10))
    first = m.next_actions()
    assert len(first) == 0                                # 12 − 10 < reserve 4
    clock.t = 0.2                                         # bucket is capped at 12
    m.cancel_reserve = 0
    assert len(m.next_actions()) == 1
    clock.t = 1.2
    assert len(m.next_actions()) == 1


def test_oms_unknown_after_5xx_resolves_by_reconcile():
    m, _ = oms()
    slot = (T, 'yes', ANCHOR)
    m.set_target(slot, Quote(20, 200))
    [a] = m.next_actions()
    m.on_result(a, False, error='timeout', status=None)
    assert m.orders[slot].state == UNKNOWN and m.next_actions() == []
    orphans = m.reconcile([{'client_order_id': a.order.coid, 'order_id': 'X', 'ticker': T,
                            'yes_price_dollars': '0.2000', 'remaining_count_fp': '200.00'},
                           {'client_order_id': 'mms-stray', 'order_id': 'Y', 'ticker': T}], is_mm_order)
    assert m.orders[slot].state == RESTING and m.orders[slot].order_id == 'X'
    assert [o['order_id'] for o in orphans] == ['Y']


def test_external_orders_count_as_own():
    m, _ = oms()
    m.reconcile([{'order_id': 'manual', 'ticker': T, 'outcome_side': 'yes', 'action': 'buy',
                  'yes_price_dollars': '0.2400', 'remaining_count': 30}], is_mm_order)
    assert m.own_levels(T, 'yes') == {24: 30.0}
    m.on_user_order({'order_id': 'manual', 'status': 'canceled', 'ticker': T}, is_mm_order)
    assert m.own_levels(T, 'yes') == {}


def test_live_gateway_converts_no_bids_to_yes_frame_asks():
    assert book_side('no') == 'ask' and yes_price('no', 55) == 45

    class C:
        order_body = staticmethod(__import__('kalshi_core').KalshiClient.order_body)

        def create_order(self, **kw):
            self.kw = kw
            return {'order_id': '1', 'remaining_count': '5.00'}

    c = C()
    m, _ = oms()
    m.set_target((T, 'no', ANCHOR), Quote(55, 5))
    [a] = m.next_actions()
    r = LiveGateway(c).execute(a, expiration_ts=123, group='g')
    assert r.ok and c.kw['book_side'] == 'ask' and c.kw['yes_price_c'] == 45
    body = C.order_body(**c.kw)
    assert body['post_only'] is True and body['expiration_time'] == 123 and body['side'] == 'ask'
    assert body['client_order_id'].startswith('mma-') and body['order_group_id'] == 'g'
