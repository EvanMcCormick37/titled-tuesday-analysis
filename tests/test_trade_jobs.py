"""Trade queue, re-plan-at-fire guards, and plan/execute behaviour.

Uses a fake Kalshi client throughout; nothing here can reach the real API.
Runs against SQLite by default, and also against Postgres when TEST_PG_URL is
set. Never point TEST_PG_URL at a database you care about: tables are dropped.
"""
from __future__ import annotations

import os
import threading
from datetime import timedelta

import pandas as pd
import pytest

from src import db, job_queue as q, strategies, trading
from src.job_runner import JobRunner, Worker
from src.trade_plan import execute_plan

DATE = '2026-11-17'
PREV = '2026-11-10'
KDATE = '26NOV17'
MARKET = 'titled_tuesday'


class FakeClient:
    """Two players on the winner market; books are configurable per test."""

    def __init__(self, asks_no_side=None, open_orders=None, fail_tickers=()):
        self.tickers = [f'KXTITLEDTUESDAY-{KDATE}-AAA', f'KXTITLEDTUESDAY-{KDATE}-BBB']
        self.names = {self.tickers[0]: 'Player A', self.tickers[1]: 'Player B'}
        self.no_dollars = asks_no_side or {}
        self.open_orders = open_orders or []
        self.fail_tickers = set(fail_tickers)
        self.placed, self.taken, self.cancelled = [], [], []
        self.on_place = None

    def get_markets_by_event(self, event_ticker):
        if event_ticker != f'KXTITLEDTUESDAY-{KDATE}':
            return []
        return [{'ticker': t, 'title': 'Winner', 'yes_sub_title': self.names[t]} for t in self.tickers]

    def get_orderbooks(self, tickers):
        return {t: {'yes_dollars': [], 'no_dollars': self.no_dollars.get(t, [])} for t in tickers}

    def get_orderbook(self, ticker):
        return self.get_orderbooks([ticker])[ticker]

    def get_open_orders(self):
        return list(self.open_orders)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.open_orders = [o for o in self.open_orders if o['order_id'] != order_id]

    def place_order(self, ticker, side, count, price, expiration_ts=None, dry_run=True):
        assert dry_run is False
        if ticker in self.fail_tickers:
            raise RuntimeError('rejected')
        self.placed.append((ticker, side, count, price))
        if self.on_place:
            self.on_place()
        return {'order': {'status': 'resting'}}

    def take_order(self, ticker, side, count, price, dry_run=True):
        assert dry_run is False
        if ticker in self.fail_tickers:
            raise RuntimeError('no liquidity')
        self.taken.append((ticker, side, count, price))
        return {'order': {'status': 'executed'}}


@pytest.fixture(params=['sqlite'] + (['postgres'] if os.environ.get('TEST_PG_URL') else []))
def backend(request, tmp_path, monkeypatch):
    if request.param == 'postgres':
        monkeypatch.setenv('DATABASE_URL', os.environ['TEST_PG_URL'])
        with db.get_conn() as conn:
            conn.execute('DROP TABLE IF EXISTS trade_job_events')
            conn.execute('DROP TABLE IF EXISTS trade_jobs')
            conn.execute('DROP TABLE IF EXISTS pipeline_runs')
            conn.execute('DROP TABLE IF EXISTS market_snapshots')
    else:
        monkeypatch.delenv('DATABASE_URL', raising=False)
        monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    q.init_schema()
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TABLE pipeline_runs (id INTEGER, market TEXT NOT NULL DEFAULT 'titled_tuesday', "
            'tourn_date TEXT, state TEXT, current_step TEXT, '
            'failed_step TEXT, error_kind TEXT, error_message TEXT, started_at TEXT, completed_at TEXT)'
        )
        conn.execute("INSERT INTO pipeline_runs (id, tourn_date, state) VALUES (1, ?, 'completed')",
                     (PREV,))
    return request.param


@pytest.fixture(autouse=True)
def model(monkeypatch):
    preds = pd.DataFrame({
        'p_participate': [0.8, 0.5],
        'P_top1_given_play': [0.5, 0.2],
        'P_top3_given_play': [0.6, 0.3],
        'P_top5_given_play': [0.7, 0.4],
        'P_top8_given_play': [0.8, 0.5],
    }, index=pd.Index(['a', 'b'], name='username'))
    monkeypatch.setattr(trading, '_load_predictions', lambda market=MARKET: preds.copy())
    monkeypatch.setattr(trading, 'get_username_mappings',
                        lambda: ({}, {'Player A': 'a', 'Player B': 'b'}))


def submit_take(backend=None, **kw):
    args = dict(market=MARKET, strategy='take_trades', tourn_date=DATE,
                params={'min_roi': 1.2, 'min_qty': 1, 'max_qty': 10}, max_exposure=100.0)
    args.update(kw)
    return strategies.submit(**args)


def submit_place(**kw):
    args = dict(market=MARKET, strategy='place_bids', tourn_date=DATE,
                params={'count': 1}, max_exposure=100.0)
    args.update(kw)
    return strategies.submit(**args)


def run_next(client, lane='normal', **kw):
    job = q.claim_next('t', lane)
    assert job is not None
    JobRunner(lambda: client, sleep_write=0, **kw).run(job)
    return q.get_job(job['id'])


# A level selling YES on Player A at 20¢: EV 0.40, ROI ≈ 1.9.
def juicy(client_ticker_index=0, price='0.80', qty='50'):
    ticker = f'KXTITLEDTUESDAY-{KDATE}-{"AAA" if client_ticker_index == 0 else "BBB"}'
    return {ticker: [[price, qty]]}


# ── Plans ────────────────────────────────────────────────────────────────────

def test_plan_includes_cancels_and_sends_nothing(backend):
    ticker = f'KXTITLEDTUESDAY-{KDATE}-AAA'
    client = FakeClient(open_orders=[{
        'order_id': 'old', 'ticker': ticker, 'outcome_side': 'yes',
        'action': 'buy', 'yes_price_dollars': '0.99',
    }])
    plan = trading.plan_place_bids(client, DATE, count=1, markup=1.5, max_discount=15,
                                   sleep_read=0)
    assert [c.order_id for c in plan.cancels] == ['old']
    assert plan.orders and not client.placed and not client.cancelled


def test_legacy_wrappers_keep_return_shape_and_dry_run(backend, capsys):
    client = FakeClient(asks_no_side=juicy())
    result = trading.take_trades(client, DATE, min_roi=1.2, min_qty=1, sleep_read=0, dry_run=True)
    assert set(result) == {'placed', 'failed', 'skipped', 'unmatched', 'edge_count',
                           'exposure', 'budget_exceeded'}
    assert result['edge_count'] == 1 and not client.taken

    result = trading.take_trades(client, DATE, min_roi=1.2, min_qty=1, sleep_read=0, dry_run=False)
    assert result['placed'] == 1 and len(client.taken) == 1


def test_failed_take_is_not_counted_as_exposure(backend):
    ticker = f'KXTITLEDTUESDAY-{KDATE}-AAA'
    client = FakeClient(asks_no_side=juicy(), fail_tickers=[ticker])
    result = trading.take_trades(client, DATE, min_roi=1.2, min_qty=1, sleep_read=0, dry_run=False)
    assert result['failed'] == 1 and result['exposure'] == 0


def test_killed_fok_counts_as_failure(backend):
    client = FakeClient(asks_no_side=juicy())
    client.take_order = lambda **kw: {'order': {'status': 'canceled'}}
    plan = trading.plan_take_trades(client, DATE, min_roi=1.2, min_qty=1, sleep_read=0)
    result = execute_plan(client, plan, sleep_write=0)
    assert result.failed == 1 and result.filled_exposure == 0


# ── Queue mechanics ──────────────────────────────────────────────────────────

def test_one_running_job_per_market_then_fifo(backend):
    first, second = submit_place(), submit_place()
    claimed = q.claim_next('t')
    assert claimed['id'] == first
    assert q.claim_next('t') is None
    q.finish(first, 'succeeded')
    assert q.claim_next('t')['id'] == second


def test_database_rejects_two_running_jobs(backend):
    a, b = submit_place(), submit_place()
    assert q.claim_next('t')['id'] == a
    with pytest.raises(Exception) as err:
        with db.get_conn() as conn:
            conn.execute("UPDATE trade_jobs SET status='running' WHERE id=?", (b,))
    assert q._is_unique_violation(err.value)


def test_concurrent_claims_never_double_start(backend):
    if backend == 'sqlite':
        pytest.skip('SQLite serialises writers; the race only exists on Postgres')
    for _ in range(5):
        submit_place()
    results, barrier = [], threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(q.claim_next('t'))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in results if r]) == 1


def test_future_job_waits_for_run_at(backend):
    now = q.utcnow()
    submit_place(run_at=now + timedelta(hours=1))
    assert q.claim_next('t') is None
    with db.get_conn() as conn:
        conn.execute('UPDATE trade_jobs SET run_at=?', (q.iso(now - timedelta(seconds=1)),))
    assert q.claim_next('t') is not None


def test_cancel_queued_job_is_immediate(backend):
    job_id = submit_place()
    assert q.request_cancel(job_id) == 'cancelled'
    assert q.claim_next('t') is None
    assert q.request_cancel(job_id) is None


# ── Submission validation ────────────────────────────────────────────────────

@pytest.mark.parametrize('kw, match', [
    ({'max_exposure': None}, 'max exposure'),
    ({'max_exposure': 0}, 'max exposure'),
    ({'loop_interval_min': 5, 'loop_fallback_hours': 1, 'strategy': 'place_bids',
      'params': {}}, 'cannot run as a loop'),
    ({'loop_interval_min': 5}, 'positive interval'),
])
def test_submit_rejects_bad_requests(backend, kw, match):
    with pytest.raises(strategies.SubmissionError, match=match):
        submit_take(**kw)


def test_submit_rejects_unknown_params(backend):
    with pytest.raises(ValueError):
        submit_take(params={'bogus': 1})


def test_cancel_needs_no_exposure_cap(backend):
    assert strategies.submit(market=MARKET, strategy='cancel_bids', tourn_date=DATE, params={})


# ── Guards at fire time ──────────────────────────────────────────────────────

def test_replan_over_cap_holds_for_review_then_approval_runs(backend):
    client = FakeClient(asks_no_side=juicy())           # 10 × 20¢ = $2.00
    job_id = submit_take(max_exposure=1.0)
    job = run_next(client)
    assert job['status'] == 'needs_review' and '$2.00' in job['review_reason']
    assert not client.taken

    assert q.approve_review(job_id, max_exposure=5.0)
    job = run_next(client)
    assert job['status'] == 'succeeded' and job['spent'] == pytest.approx(2.0)
    assert len(client.taken) == 1


def test_stale_predictions_hold_unless_overridden(backend):
    with db.get_conn() as conn:
        conn.execute("UPDATE pipeline_runs SET state='adjustments_pending'")
    client = FakeClient(asks_no_side=juicy())
    job_id = submit_take()
    assert run_next(client)['status'] == 'needs_review'
    assert not client.taken

    q.approve_review(job_id, override_stale=True)
    assert run_next(client)['status'] == 'succeeded'
    assert len(client.taken) == 1


def test_guard_uses_prior_week_on_tournament_morning(backend):
    """The row for tourn_date itself doesn't exist until after that tournament."""
    assert strategies.MARKETS[MARKET].pipeline_ready(DATE)
    assert not strategies.MARKETS[MARKET].pipeline_ready('2026-11-24')


def test_missed_window_expires_without_trading(backend):
    client = FakeClient(asks_no_side=juicy())
    job_id = submit_take()
    with db.get_conn() as conn:
        conn.execute('UPDATE trade_jobs SET not_after=?', (q.iso(q.utcnow() - timedelta(minutes=1)),))
    assert run_next(client)['status'] == 'expired'
    assert not client.taken
    assert q.get_job(job_id)['status'] == 'expired'


def test_dry_run_job_sends_nothing(backend):
    client = FakeClient(asks_no_side=juicy())
    submit_take(dry_run=True, max_exposure=None)
    job = run_next(client)
    assert job['status'] == 'succeeded' and job['plan']['orders']
    assert not client.taken


# ── Outcomes ─────────────────────────────────────────────────────────────────

def test_mixed_results_are_partial_and_only_accepted_orders_count(backend):
    bad = f'KXTITLEDTUESDAY-{KDATE}-BBB'
    client = FakeClient(fail_tickers=[bad])
    submit_place(params={'count': 1, 'side_filter': 'yes'})
    job = run_next(client)
    assert job['status'] == 'partial'
    assert job['summary']['failed'] == 1 and job['summary']['placed'] == 1
    expected = client.placed[0][2] * client.placed[0][3] / 100
    assert job['spent'] == pytest.approx(expected)
    assert job['progress_done'] == job['progress_total'] == 2
    assert any(e['level'] == 'error' for e in q.list_events(job['id']))


def test_all_failed_is_failed(backend):
    client = FakeClient(fail_tickers=client_tickers())
    submit_place(params={'count': 1, 'side_filter': 'yes'})
    assert run_next(client)['status'] == 'failed'


def client_tickers():
    return [f'KXTITLEDTUESDAY-{KDATE}-AAA', f'KXTITLEDTUESDAY-{KDATE}-BBB']


def test_exception_is_recorded_and_job_fails(backend):
    client = FakeClient()
    client.get_open_orders = lambda: (_ for _ in ()).throw(RuntimeError('kalshi down'))
    submit_place()
    job = run_next(client)
    assert job['status'] == 'failed' and 'kalshi down' in job['error']


def test_cancel_mid_run_stops_between_orders(backend):
    client = FakeClient()
    job_id = submit_place(params={'count': 1, 'side_filter': 'yes'})
    client.on_place = lambda: q.request_cancel(job_id)
    job = run_next(client)
    assert job['status'] == 'cancelled' and len(client.placed) == 1


# ── Loops ────────────────────────────────────────────────────────────────────

def submit_loop(**kw):
    return submit_take(loop_interval_min=5, loop_fallback_hours=1, **kw)


def test_loop_requeues_without_holding_the_lane(backend):
    client = FakeClient(asks_no_side=juicy())
    submit_loop(max_exposure=10.0)
    job = run_next(client)
    assert job['status'] == 'queued' and job['iterations'] == 1
    assert job['spent'] == pytest.approx(2.0)
    assert q.iso(q.utcnow()) < job['run_at']

    other = submit_place()
    assert q.claim_next('t')['id'] == other


def test_loop_backs_off_when_dry(backend):
    client = FakeClient()
    submit_loop()
    job = run_next(client)
    wait = pd.Timestamp(job['run_at']) - pd.Timestamp(q.iso(q.utcnow()))
    assert wait > pd.Timedelta(minutes=50)


def test_loop_ends_when_budget_exhausted(backend):
    client = FakeClient(asks_no_side=juicy())
    job_id = submit_loop(max_exposure=3.0)
    assert run_next(client)['status'] == 'queued'
    with db.get_conn() as conn:
        conn.execute('UPDATE trade_jobs SET run_at=?', (q.iso(q.utcnow()),))
    job = run_next(client)
    assert job['status'] == 'succeeded' and job['summary']['stop_reason'] == 'budget-exhausted'
    assert len(client.taken) == 1 and q.get_job(job_id)['spent'] == pytest.approx(2.0)


def test_loop_fails_after_three_consecutive_errors(backend):
    client = FakeClient()
    client.get_markets_by_event = lambda *_: (_ for _ in ()).throw(RuntimeError('boom'))
    submit_loop()
    for expected in ('queued', 'queued', 'failed'):
        job = run_next(client)
        assert job['status'] == expected
        with db.get_conn() as conn:
            conn.execute('UPDATE trade_jobs SET run_at=?', (q.iso(q.utcnow()),))


def test_failed_fok_does_not_consume_loop_budget(backend):
    client = FakeClient(asks_no_side=juicy(), fail_tickers=client_tickers())
    submit_loop(max_exposure=10.0)
    job = run_next(client)
    assert job['status'] == 'queued' and job['spent'] == 0


# ── Kill switch ──────────────────────────────────────────────────────────────

def test_kill_clears_queue_stops_running_and_jumps_lane(backend):
    ticker = f'KXTITLEDTUESDAY-{KDATE}-AAA'
    client = FakeClient(open_orders=[{'order_id': 'x', 'ticker': ticker, 'outcome_side': 'yes'}])
    running, queued = submit_place(), submit_place()
    assert q.claim_next('t')['id'] == running

    kill_id = q.enqueue_kill(MARKET, DATE)
    assert q.get_job(queued)['status'] == 'cancelled'
    assert q.get_job(running)['cancel_requested'] is True

    q.finish(running, 'cancelled')
    job = run_next(client, lane='priority', kill_idle_timeout=0.1)
    assert job['id'] == kill_id and job['status'] == 'succeeded'
    assert client.cancelled == ['x']


def test_kill_sweeps_again_after_running_job_stops(backend):
    ticker = f'KXTITLEDTUESDAY-{KDATE}-AAA'
    client = FakeClient(open_orders=[{'order_id': 'first', 'ticker': ticker, 'outcome_side': 'yes'}])
    running = submit_place()
    q.claim_next('t')
    q.enqueue_kill(MARKET, DATE)
    job = q.claim_next('t', 'priority')

    original_cancel = client.cancel_order

    def cancel_then_late_order(order_id):
        original_cancel(order_id)
        if order_id == 'first':
            client.open_orders.append({'order_id': 'late', 'ticker': ticker, 'outcome_side': 'yes'})
            q.finish(running, 'cancelled')

    client.cancel_order = cancel_then_late_order
    JobRunner(lambda: client, sleep_write=0).run(job)
    assert client.cancelled == ['first', 'late']
    assert q.get_job(job['id'])['summary']['cancelled'] == 2


# ── Worker ───────────────────────────────────────────────────────────────────

def test_stale_running_job_is_interrupted_and_never_rerun(backend):
    job_id = submit_place()
    q.claim_next('t')
    with db.get_conn() as conn:
        conn.execute('UPDATE trade_jobs SET heartbeat_at=?', (q.iso(q.utcnow() - timedelta(minutes=5)),))
    assert q.reap_stale(60) == [job_id]
    assert q.get_job(job_id)['status'] == 'interrupted'
    assert q.claim_next('t') is None


def test_fresh_heartbeat_is_not_reaped(backend):
    submit_place()
    job = q.claim_next('t')
    q.heartbeat([job['id']])
    assert q.reap_stale(60) == []


def test_worker_tick_runs_job_end_to_end(backend):
    client = FakeClient(asks_no_side=juicy())
    job_id = submit_take()
    worker = Worker(client_factory=lambda: client, sleep_write=0)
    assert worker.tick() == 1
    worker.drain(10)
    job = q.get_job(job_id)
    assert job['status'] == 'succeeded' and len(client.taken) == 1
    kinds = [e['kind'] for e in q.list_events(job_id)]
    assert 'plan' in kinds and 'order' in kinds


def test_shutdown_mid_job_is_interrupted_not_cancelled(backend):
    client = FakeClient()
    stopping = threading.Event()
    client.on_place = stopping.set
    job_id = submit_place(params={'count': 1, 'side_filter': 'yes'})
    JobRunner(lambda: client, shutting_down=stopping.is_set, sleep_write=0).run(q.claim_next('t'))
    job = q.get_job(job_id)
    assert job['status'] == 'interrupted' and len(client.placed) == 1
