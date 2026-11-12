"""Markets must not see each other's data, and the migration must not lose any."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from src import db, strategies, trading
from src.data import _conflict_z_deduction
from src.markets import BULLET_BRAWL, TITLED_TUESDAY
from src.multi_market_schema import migrate
from src.pipeline import load_adjustments
from src.pipeline_runner import PipelineDef, run_pipeline
from src.trade_plan import noop_emit

TT, BB = TITLED_TUESDAY.slug, BULLET_BRAWL.slug
_PREDICTION_COLS = 'username TEXT, p_participate REAL, "P_top1_given_play" REAL, tourn_date TEXT'
_TABLES = ('job_runs', 'pipeline_runs', 'manual_adjustments', 'latest_model_predictions',
           'latest_model_predictions_raw', 'historical_predictions', 'backtest',
           'attendance_conflicts', 'bullet_brawl_standings', 'bullet_brawl_tournaments')


def _old_schema(conn) -> None:
    pk = 'BIGSERIAL PRIMARY KEY' if db.is_postgres() else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    for stmt in (
        f"""CREATE TABLE pipeline_runs (
                id {pk}, tourn_date TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('running','adjustments_pending','completed','failed')),
                current_step TEXT, failed_step TEXT, error_kind TEXT, error_message TEXT,
                started_at TEXT, completed_at TEXT)""",
        f"""CREATE TABLE job_runs (
                id {pk}, pipeline_run_id BIGINT REFERENCES pipeline_runs(id), job_name TEXT NOT NULL,
                started_at TEXT, ended_at TEXT, status TEXT NOT NULL, summary TEXT, log_tail TEXT)""",
        f"""CREATE TABLE manual_adjustments (
                id {pk}, tourn_date TEXT NOT NULL, player_name TEXT NOT NULL,
                adjustment_type TEXT NOT NULL, value REAL, note TEXT, created_at TEXT, created_by TEXT)""",
        f'CREATE TABLE latest_model_predictions ({_PREDICTION_COLS})',
        f'CREATE TABLE latest_model_predictions_raw ({_PREDICTION_COLS})',
        """CREATE TABLE historical_predictions (
                tourn_date TEXT NOT NULL, username TEXT NOT NULL, p_participate REAL,
                PRIMARY KEY (tourn_date, username))""",
        """CREATE TABLE backtest (
                tourn_date TEXT NOT NULL, model TEXT NOT NULL, username TEXT NOT NULL, played INTEGER,
                PRIMARY KEY (tourn_date, model, username))""",
        """CREATE TABLE attendance_conflicts (
                date TEXT NOT NULL, player_name TEXT NOT NULL, username TEXT NOT NULL,
                PRIMARY KEY (date, username))""",
        "INSERT INTO pipeline_runs (tourn_date, state) VALUES ('2026-11-03', 'completed')",
        "INSERT INTO job_runs (pipeline_run_id, job_name, status) VALUES (1, 'run_raw', 'success')",
        "INSERT INTO manual_adjustments (tourn_date, player_name, adjustment_type) VALUES ('2026-11-10', 'A', 'cut')",
        "INSERT INTO latest_model_predictions VALUES ('a', 0.5, 0.25, '2026-11-10')",
        "INSERT INTO latest_model_predictions_raw VALUES ('a', 0.6, 0.25, '2026-11-10')",
        "INSERT INTO historical_predictions VALUES ('2026-11-03', 'a', 0.5)",
        "INSERT INTO backtest VALUES ('2026-11-03', 'score', 'a', 1)",
        "INSERT INTO attendance_conflicts VALUES ('2026-11-03', 'A', 'a')",
    ):
        conn.execute(stmt)


@pytest.fixture(params=['sqlite'] + (['postgres'] if os.environ.get('TEST_PG_URL') else []))
def database(request, tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_ENGINE', None)
    if request.param == 'postgres':
        monkeypatch.setenv('DATABASE_URL', os.environ['TEST_PG_URL'])
        with db.get_conn() as conn:
            for table in _TABLES:
                conn.execute(f'DROP TABLE IF EXISTS {table} CASCADE')
    else:
        monkeypatch.delenv('DATABASE_URL', raising=False)
        monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    with db.get_conn() as conn:
        _old_schema(conn)
    with db.get_conn() as conn:
        migrate(conn)
    yield request.param
    if db._ENGINE is not None:
        db._ENGINE.dispose()
    if request.param == 'postgres':
        with db.get_conn() as conn:
            for table in _TABLES:
                conn.execute(f'DROP TABLE IF EXISTS {table} CASCADE')


def _count(table: str, where: str = '1=1') -> int:
    with db.get_conn() as conn:
        return conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}').fetchone()[0]


# ── Migration ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('table', ['pipeline_runs', 'manual_adjustments', 'latest_model_predictions',
                                   'latest_model_predictions_raw', 'historical_predictions',
                                   'backtest', 'attendance_conflicts'])
def test_existing_rows_become_titled_tuesday(database, table):
    assert _count(table) == 1 and _count(table, f"market = '{TT}'") == 1


def test_migration_is_idempotent(database):
    with db.get_conn() as conn:
        migrate(conn)
    assert _count('pipeline_runs') == 1 and _count('attendance_conflicts') == 1


def test_job_run_link_survives(database):
    with db.get_conn() as conn:
        row = conn.execute('SELECT p.tourn_date FROM job_runs j JOIN pipeline_runs p '
                           'ON j.pipeline_run_id = p.id').fetchone()
    assert row[0] == '2026-11-03'


def test_same_date_allowed_per_market_but_not_within_one(database):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO pipeline_runs (market, tourn_date, state) VALUES (?, '2026-11-03', 'running')", (BB,))
    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute("INSERT INTO pipeline_runs (market, tourn_date, state) VALUES (?, '2026-11-03', 'running')", (BB,))


def test_previous_release_can_still_insert(database):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO pipeline_runs (tourn_date, state) VALUES ('2026-11-10', 'running')")
    assert _count('pipeline_runs', f"market = '{TT}'") == 2


def test_bullet_brawl_tables_exist_without_a_games_table(database):
    with db.get_conn() as conn:
        assert 'score' in db.table_columns(conn, 'bullet_brawl_standings')
        assert 'winner' in db.table_columns(conn, 'bullet_brawl_tournaments')
        assert not db.table_columns(conn, 'bullet_brawl_games')


# ── Isolation ────────────────────────────────────────────────────────────────

def _preds(p: float) -> pd.DataFrame:
    return pd.DataFrame({'username': ['x'], 'p_participate': [p], 'P_top1_given_play': [0.1],
                         'tourn_date': ['2026-11-14']})


def test_replacing_one_market_keeps_the_other(database):
    db.replace_market_rows(_preds(0.9), 'latest_model_predictions', BB)
    db.replace_market_rows(_preds(0.8), 'latest_model_predictions', BB)
    assert db.read_market_rows('latest_model_predictions', BB)['p_participate'].tolist() == [pytest.approx(0.8)]
    tt = db.read_market_rows('latest_model_predictions', TT)
    assert tt['username'].tolist() == ['a'] and 'market' not in tt.columns


def test_adjustments_are_scoped(database):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO manual_adjustments (market, tourn_date, player_name, adjustment_type) "
                     "VALUES (?, '2026-11-10', 'B', 'cut')", (BB,))
    assert load_adjustments('2026-11-10')['cut_players'] == ['A']
    assert load_adjustments('2026-11-10', BB)['cut_players'] == ['B']


def test_conflicts_from_another_market_do_not_change_attendance(database):
    ref = pd.Timestamp('2026-11-20')
    with db.get_conn() as conn:
        before = _conflict_z_deduction(conn, ref, False, None)
        conn.execute("INSERT INTO attendance_conflicts (market, date, player_name, username) "
                     "VALUES (?, '2026-11-07', 'A', 'a')", (BB,))
        after = _conflict_z_deduction(conn, ref, False, None)
    assert before == after and before['a'] > 0


def test_pipeline_ready_is_scoped(database):
    tt_market = strategies.MARKETS[TT]
    assert tt_market.pipeline_ready('2026-11-10')
    assert not strategies._pipeline_ready(BULLET_BRAWL, '2026-11-10')


# ── Pipeline runner ──────────────────────────────────────────────────────────

def _defn(run_step):
    return PipelineDef(BULLET_BRAWL, ('one', 'two'), run_step, lambda step, exc: 'unknown')


def test_runner_writes_only_its_own_market(database):
    final = run_pipeline(_defn(lambda step, slug: {'step': step}))
    assert final['state'] == 'adjustments_pending'
    assert _count('pipeline_runs', f"market = '{BB}'") == 1
    assert _count('pipeline_runs', f"market = '{TT}' AND state = 'completed'") == 1


def test_runner_resumes_from_failed_step(database):
    calls = []

    def flaky(step, slug):
        calls.append(step)
        if step == 'two' and calls.count('two') == 1:
            raise RuntimeError('boom')
        return {}

    failed = run_pipeline(_defn(flaky))
    assert failed['state'] == 'failed' and failed['failed_step'] == 'two'
    assert run_pipeline(_defn(flaky))['state'] == 'adjustments_pending'
    assert calls == ['one', 'two', 'two']


# ── Specs ────────────────────────────────────────────────────────────────────

def test_titled_tuesday_tickers_unchanged():
    assert list(TITLED_TUESDAY.event_templates.items()) == [
        ('KXTITLEDTUESDAY-{date}', 1), ('KXTITLEDTUESTOP-{date}T3', 3),
        ('KXTITLEDTUESTOP-{date}T5', 5), ('KXTITLEDTUESTOP-{date}T8', 8),
    ]


@pytest.mark.parametrize('ticker, n', [
    ('KXBULLETBRAWL-26NOV14-HNAK', 1), ('KXBULLETBRAWLTOP-26NOV14T8-HNAK', 8),
    ('KXBULLETBRAWLTOP-26NOV14T12-HNAK', 12), ('KXBULLETBRAWL-26NOV07-HNAK', None),
    ('KXTITLEDTUESDAY-26NOV14-HNAK', None),
])
def test_ticker_matching_stays_inside_the_market_and_date(ticker, n):
    assert bool(BULLET_BRAWL.ticker_pattern('26NOV14').search(ticker)) == (n is not None)
    assert BULLET_BRAWL.n_from_ticker(ticker, '26NOV14') == n


@pytest.mark.parametrize('event, expected', [
    ('2026-07-25', datetime(2026, 7, 25, 16, tzinfo=timezone.utc)),     # EDT
    ('2026-11-14', datetime(2026, 11, 14, 17, tzinfo=timezone.utc)),    # EST
])
def test_bullet_brawl_start_handles_dst(event, expected):
    assert datetime.fromtimestamp(BULLET_BRAWL.start_ts(event), timezone.utc) == expected


def test_event_date_helpers():
    assert BULLET_BRAWL.next_event_on_or_after(date(2026, 11, 9)) == date(2026, 11, 14)
    assert BULLET_BRAWL.next_event_on_or_after(date(2026, 11, 14)) == date(2026, 11, 14)
    assert BULLET_BRAWL.most_recent_event(date(2026, 11, 13)) == date(2026, 11, 7)
    assert TITLED_TUESDAY.most_recent_event(date(2026, 11, 9)) == date(2026, 11, 3)


def test_bullet_brawl_is_registered_with_its_own_spec():
    market = strategies.MARKETS[BB]
    assert market.spec is BULLET_BRAWL
    assert set(market.strategies) == set(strategies.MARKETS[TT].strategies)
    assert BULLET_BRAWL.pipeline_steps[0] == 'scrape_bb'
    assert BULLET_BRAWL.model.score_noise_sigma == 10.0
    assert TITLED_TUESDAY.model.score_noise_sigma == 0.0


def test_every_market_has_a_pipeline():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(root / 'scripts'), str(root / 'scripts' / 'scraping')]
    import weekly_pipeline
    assert set(weekly_pipeline.PIPELINES) == set(strategies.MARKETS)
    for slug, defn in weekly_pipeline.PIPELINES.items():
        assert defn.steps == strategies.MARKETS[slug].spec.pipeline_steps


class _BrawlClient:
    def get_markets_by_event(self, event):
        return [{'ticker': f'{event}-HNAK', 'title': 'x', 'yes_sub_title': 'Player A'}]

    def get_orderbooks(self, tickers):
        return {t: {'yes_dollars': [], 'no_dollars': []} for t in tickers}

    def get_open_orders(self):
        return [{'order_id': 'bb', 'ticker': 'KXBULLETBRAWLTOP-26NOV14T3-HNAK', 'yes_price_dollars': '0.01'},
                {'order_id': 'tt', 'ticker': 'KXTITLEDTUESDAY-26NOV14-HNAK', 'yes_price_dollars': '0.01'}]


def test_trading_uses_the_spec(monkeypatch):
    preds = pd.DataFrame({f'P_top{n}_given_play': [0.5] for n in (1, 3, 5, 8)} | {'p_participate': [1.0]},
                         index=pd.Index(['a'], name='username'))
    seen = []
    monkeypatch.setattr(trading, '_load_predictions', lambda market: seen.append(market) or preds)
    monkeypatch.setattr(trading, 'market_player_to_username', lambda usernames, spec: {'Player A': 'a'})
    client = _BrawlClient()
    plan = trading.plan_place_bids(client, '2026-11-14', count=1, sleep_read=0, spec=BULLET_BRAWL)
    assert seen == [BB]
    assert {o.ticker for o in plan.orders} == {
        'KXBULLETBRAWL-26NOV14-HNAK', 'KXBULLETBRAWLTOP-26NOV14T3-HNAK',
        'KXBULLETBRAWLTOP-26NOV14T5-HNAK', 'KXBULLETBRAWLTOP-26NOV14T8-HNAK',
    }
    assert {o.expiration_ts for o in plan.orders} == {BULLET_BRAWL.bid_expiry_ts('2026-11-14')}
    cancel = trading.plan_cancel_bids(client, '2026-11-14', noop_emit, spec=BULLET_BRAWL)
    assert [c.order_id for c in cancel.cancels] == ['bb']
