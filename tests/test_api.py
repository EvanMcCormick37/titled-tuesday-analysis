"""HTTP-level tests for tt-api, plus ops jobs and snapshots. No real Kalshi calls."""
from __future__ import annotations

import sys
from functools import partial

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import auth
from api import main as api_main
from src import db, job_queue as q, strategies
from src.job_runner import JobRunner, Worker
from src.markets import TITLED_TUESDAY
from tests.test_trade_jobs import DATE, MARKET, PREV, FakeClient, backend, juicy, model  # noqa: F401

PASSWORD = 'correct horse'


@pytest.fixture
def client(backend, monkeypatch):  # noqa: F811
    auth._failures.clear()
    monkeypatch.setenv('DASHBOARD_PASSWORD', PASSWORD)
    market = strategies.MARKETS[MARKET]
    object.__setattr__(market, 'next_event_date', lambda: DATE)
    fake = FakeClient(asks_no_side=juicy())
    monkeypatch.setattr(api_main, '_kalshi', lambda: fake)
    with TestClient(api_main.app, base_url='https://testserver') as c:
        c.fake = fake
        yield c
    object.__setattr__(market, 'next_event_date', partial(strategies._next_date, market.spec))


@pytest.fixture
def authed(client):
    assert client.post('/api/login', json={'password': PASSWORD}).status_code == 200
    return client


# ── Auth ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('method, path', [
    ('get', '/api/markets'),
    ('get', f'/api/markets/{MARKET}/status'),
    ('get', f'/api/markets/{MARKET}/jobs'),
    ('post', f'/api/markets/{MARKET}/jobs'),
    ('post', f'/api/markets/{MARKET}/kill'),
    ('post', f'/api/markets/{MARKET}/preview'),
    ('post', '/api/jobs/1/cancel'),
    ('post', '/api/jobs/1/approve'),
    ('put', f'/api/markets/{MARKET}/adjustments'),
    ('get', '/api/logs'),
])
def test_every_route_requires_login(client, method, path):
    assert client.request(method, path, json={}).status_code == 401


def test_wrong_password_rejected_and_rate_limited(client):
    for _ in range(auth._MAX_FAILURES):
        assert client.post('/api/login', json={'password': 'nope'}).status_code == 401
    assert client.post('/api/login', json={'password': PASSWORD}).status_code == 429


def test_login_fails_closed_without_configured_password(client, monkeypatch):
    monkeypatch.delenv('DASHBOARD_PASSWORD')
    assert client.post('/api/login', json={'password': ''}).status_code == 503
    assert client.get('/api/markets').status_code == 401


def test_forged_cookie_rejected(client):
    client.cookies.set(auth.COOKIE_NAME, '9999999999.deadbeef')
    assert client.get('/api/markets').status_code == 401


def test_cookie_flags(client):
    header = client.post('/api/login', json={'password': PASSWORD}).headers['set-cookie'].lower()
    assert 'httponly' in header and 'samesite=strict' in header and 'secure' in header


# ── Trading flow ─────────────────────────────────────────────────────────────

def test_preview_submit_run_detail(authed):
    body = {'strategy': 'take_trades', 'params': {'min_roi': 1.2, 'min_qty': 1, 'max_qty': 10}}
    plan = authed.post(f'/api/markets/{MARKET}/preview', json=body).json()
    assert plan['exposure'] == pytest.approx(2.0) and not authed.fake.taken

    job_id = authed.post(f'/api/markets/{MARKET}/jobs',
                         json={**body, 'max_exposure': plan['exposure']}).json()['id']
    listed = authed.get(f'/api/markets/{MARKET}/jobs').json()
    assert listed[0]['id'] == job_id and listed[0]['status'] == 'queued'
    assert 'plan' not in listed[0]

    JobRunner(lambda: authed.fake, sleep_write=0).run(q.claim_next('t'))
    detail = authed.get(f'/api/jobs/{job_id}').json()
    assert detail['job']['status'] == 'succeeded' and len(authed.fake.taken) == 1
    last = detail['events'][-1]['id']
    assert authed.get(f'/api/jobs/{job_id}?after={last}').json()['events'] == []


def test_status_and_history_ignore_other_markets(authed):
    with db.get_conn() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS job_runs (id INTEGER, pipeline_run_id INTEGER, job_name TEXT, '
                     'status TEXT, started_at TEXT, ended_at TEXT, summary TEXT, log_tail TEXT)')
        conn.execute("INSERT INTO pipeline_runs (id, market, tourn_date, state) "
                     "VALUES (2, 'bullet_brawl', '2026-11-14', 'failed')")
    assert authed.get(f'/api/markets/{MARKET}/status').json()['pipeline']['tourn_date'] == PREV
    assert [r['tourn_date'] for r in authed.get(f'/api/markets/{MARKET}/pipeline').json()['runs']] == [PREV]


def test_markets_advertise_their_n_values(authed):
    listed = authed.get('/api/markets').json()[0]
    assert listed['n_values'] == [1, 3, 5, 8, 10] and listed['traded_ns'] == [1, 3, 5, 8]


def test_submit_without_cap_is_422_with_message(authed):
    r = authed.post(f'/api/markets/{MARKET}/jobs', json={'strategy': 'take_trades', 'params': {}})
    assert r.status_code == 422 and 'max exposure' in r.json()['detail']


def test_bad_params_are_422(authed):
    r = authed.post(f'/api/markets/{MARKET}/preview',
                    json={'strategy': 'take_trades', 'params': {'min_roi': 99}})
    assert r.status_code == 422 and 'min_roi' in r.json()['detail']


def test_naive_run_at_rejected(authed):
    r = authed.post(f'/api/markets/{MARKET}/jobs', json={
        'strategy': 'take_trades', 'params': {}, 'max_exposure': 5, 'run_at': '2026-11-17T10:45:00'})
    assert r.status_code == 422 and 'timezone' in r.json()['detail']


def test_review_approve_over_http(authed):
    job_id = authed.post(f'/api/markets/{MARKET}/jobs', json={
        'strategy': 'take_trades', 'params': {'min_roi': 1.2, 'min_qty': 1, 'max_qty': 10},
        'max_exposure': 1.0}).json()['id']
    JobRunner(lambda: authed.fake, sleep_write=0).run(q.claim_next('t'))
    assert authed.get(f'/api/jobs/{job_id}').json()['job']['status'] == 'needs_review'
    assert authed.post(f'/api/jobs/{job_id}/approve', json={'max_exposure': 5}).status_code == 200
    assert authed.post(f'/api/jobs/{job_id}/approve', json={}).status_code == 409


def test_status_uses_prior_week_guard_and_reports_worker(authed):
    data = authed.get(f'/api/markets/{MARKET}/status').json()
    assert data['event_date'] == DATE and data['trade_ready'] is True
    assert data['pipeline']['tourn_date'] == PREV and data['worker_online'] is False


# ── Ops jobs ─────────────────────────────────────────────────────────────────

def _register_op(monkeypatch, code: str, verify=lambda: None):
    op = strategies.OpsJob('recompute', 'Recompute', lambda d, p: [sys.executable, '-c', code], verify)
    monkeypatch.setitem(strategies.MARKETS[MARKET].ops, 'recompute', op)


@pytest.fixture
def date_patched(backend):  # noqa: F811
    market = strategies.MARKETS[MARKET]
    object.__setattr__(market, 'next_event_date', lambda: DATE)
    yield
    object.__setattr__(market, 'next_event_date', partial(strategies._next_date, market.spec))


def _run_op():
    job = q.claim_next('t')
    JobRunner(lambda: None, ops_poll=0.02).run(job)
    return q.get_job(job['id'])


def test_ops_success_captures_output(date_patched, monkeypatch):
    _register_op(monkeypatch, 'print("hello from sim")')
    strategies.submit_ops(MARKET, 'recompute')
    job = _run_op()
    assert job['status'] == 'succeeded'
    log = [e for e in q.list_events(job['id']) if e['kind'] == 'log'][0]
    assert 'hello from sim' in log['data']['tail']


def test_ops_nonzero_exit_fails(date_patched, monkeypatch):
    _register_op(monkeypatch, 'import sys; print("bad thing"); sys.exit(2)')
    strategies.submit_ops(MARKET, 'recompute')
    job = _run_op()
    assert job['status'] == 'failed' and 'bad thing' in job['error']


def test_ops_verify_catches_recorded_failure(date_patched, monkeypatch):
    _register_op(monkeypatch, 'pass', verify=lambda: 'scrape failed')
    strategies.submit_ops(MARKET, 'recompute')
    job = _run_op()
    assert job['status'] == 'failed' and job['error'] == 'scrape failed'


def test_duplicate_ops_refused(date_patched):
    strategies.submit_ops(MARKET, 'recompute')
    with pytest.raises(strategies.SubmissionError, match='already'):
        strategies.submit_ops(MARKET, 'recompute')


def test_recompute_blocks_trades_until_done(date_patched):
    ops_id = strategies.submit_ops(MARKET, 'recompute')
    assert q.claim_next('t')['id'] == ops_id
    strategies.submit(market=MARKET, strategy='place_bids', tourn_date=DATE, params={}, max_exposure=5)
    assert q.claim_next('t') is None


def test_kill_leaves_ops_alone(date_patched):
    ops_id = strategies.submit_ops(MARKET, 'recompute')
    q.claim_next('t')
    q.enqueue_kill(MARKET, DATE)
    job = q.get_job(ops_id)
    assert job['status'] == 'running' and job['cancel_requested'] is False


def test_ops_command_cannot_inject_flags():
    cmd = strategies.MARKETS[MARKET].ops['run_pipeline'].command(DATE, {'slug_url': '--resume=run_raw'})
    assert cmd[-1] == '--slug-url=--resume=run_raw'


# ── Snapshots ────────────────────────────────────────────────────────────────

def test_snapshot_only_refreshes_while_viewed(backend):  # noqa: F811
    assert not q.snapshot_is_due(MARKET, 'edges', 15)
    q.request_snapshot(MARKET, 'edges')
    assert q.snapshot_is_due(MARKET, 'edges', 15)
    q.save_snapshot(MARKET, 'edges', {'rows': [1]})
    assert not q.snapshot_is_due(MARKET, 'edges', 15)
    assert q.snapshot_is_due(MARKET, 'edges', 0)


def test_force_refresh_and_failure_keeps_last_payload(backend):  # noqa: F811
    q.request_snapshot(MARKET, 'edges')
    q.save_snapshot(MARKET, 'edges', {'rows': [1]})
    q.request_snapshot(MARKET, 'edges', force=True)
    assert q.snapshot_is_due(MARKET, 'edges', 999)
    q.save_snapshot(MARKET, 'edges', None, error='kalshi down')
    snap = q.request_snapshot(MARKET, 'edges')
    assert snap['payload'] == {'rows': [1]} and snap['error'] == 'kalshi down'


def test_worker_publishes_snapshot(date_patched):
    fake = FakeClient(asks_no_side=juicy())
    q.request_snapshot(MARKET, 'edges')
    worker = Worker(client_factory=lambda: fake, sleep_write=0)
    worker.tick()
    worker.drain(10)
    worker._refreshing and [f.result(5) for f in worker._refreshing.values()]
    snap = q.request_snapshot(MARKET, 'edges')
    assert snap['error'] is None and snap['payload']['rows'][0]['player'] == 'Player A'
    assert q.worker_last_seen() is not None


def test_edges_payload_is_json_safe(date_patched):
    from src import market_views
    payload = market_views.edges_snapshot(FakeClient(asks_no_side=juicy()), DATE, TITLED_TUESDAY)
    row = payload['rows'][0]
    assert row['roi'] > 1.2 and row['side'] == 'YES'
    assert pd.notna(row['ev'])
