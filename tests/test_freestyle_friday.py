"""Freestyle Friday as a market: spec, slug helpers, discovery and loading."""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src import data, db, strategies
from src.markets import FREESTYLE_FRIDAY, SPECS
from src.multi_market_schema import migrate

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts' / 'scraping')]
import update_freestyle_friday as uff  # noqa: E402

SLUG = 'freestyle-friday-november-20-2026-31235059'
START = 1795190400      # 2026-11-20 11:00 ET


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    data.invalidate_market_maps()
    with db.get_conn() as conn:
        migrate(conn)
        conn.execute('CREATE TABLE IF NOT EXISTS player_information '
                     '(username TEXT PRIMARY KEY, player_name TEXT, is_default INTEGER DEFAULT 0)')
        conn.execute('CREATE TABLE IF NOT EXISTS titled_tuesday_standings '
                     '(tournament_slug TEXT, date TEXT, username TEXT, rank REAL)')
    yield
    data.invalidate_market_maps()


# ── Spec and registration ────────────────────────────────────────────────────

def test_spec_schedule_and_tickers():
    assert FREESTYLE_FRIDAY.next_event_on_or_after(date(2026, 11, 22)) == date(2026, 11, 27)
    assert FREESTYLE_FRIDAY.most_recent_event(date(2026, 11, 22)) == date(2026, 11, 20)
    assert datetime.fromtimestamp(FREESTYLE_FRIDAY.start_ts('2026-11-20'), timezone.utc) == \
        datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
    assert FREESTYLE_FRIDAY.n_from_ticker('KXFSFRIDAY-26NOV27-MCAR', '26NOV27') == 1
    assert FREESTYLE_FRIDAY.n_from_ticker('KXFSFRIDAYTOP-26NOV27T8-MCAR', '26NOV27') == 8
    # The winner series is a prefix of the top series; a top-N ticker must not read as N=1.
    assert FREESTYLE_FRIDAY.n_from_ticker('KXFSFRIDAYTOP-26NOV27T3-MCAR', '26NOV27') == 3
    assert FREESTYLE_FRIDAY.ticker_pattern('26NOV27').search('KXFSFRIDAYTOP-26NOV27T5-HNAK')


def test_registered_everywhere():
    import weekly_pipeline
    assert SPECS['freestyle_friday'] is FREESTYLE_FRIDAY
    assert strategies.MARKETS['freestyle_friday'].spec is FREESTYLE_FRIDAY
    assert weekly_pipeline.PIPELINES['freestyle_friday'].steps[0] == 'scrape_ff'
    assert FREESTYLE_FRIDAY.is_event_url(f'https://www.chess.com/tournament/live/{SLUG}')
    assert not FREESTYLE_FRIDAY.is_event_url('titled-tuesday-blitz-november-17-2026-31235051')


# ── Slugs ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    SLUG,
    f'https://www.chess.com/tournament/live/{SLUG}',
    f'https://www.chess.com/tournament/live/{SLUG}?&players=2',
    f'https://api.chess.com/pub/tournament/{SLUG}',
])
def test_slug_from_url(text):
    assert uff.slug_from_url(text) == SLUG


def test_slug_from_url_rejects_other_events():
    with pytest.raises(ValueError):
        uff.slug_from_url('https://www.chess.com/tournament/live/titled-tuesday-blitz-november-17-2026-31235051')


def test_slug_date_and_archive_scan():
    assert uff.slug_date(SLUG) == date(2026, 11, 20)
    archive = {'games': [
        {'tournament': f'https://api.chess.com/pub/tournament/{SLUG}'},
        {'tournament': 'https://api.chess.com/pub/tournament/titled-tuesday-blitz-november-17-2026-31235051'},
        {},
    ]}
    assert uff.slugs_in_archive(archive) == {SLUG}


# ── Loading ──────────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, body):
        self.status_code = 404 if body is None else 200
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, routes):
        self.routes, self.headers = routes, {}

    def get(self, url, timeout=None, headers=None):
        return _Response(self.routes.get(url))


def _details(rules='chess960', start_time=START, status='finished'):
    return {'status': status, 'start_time': start_time, 'finish_time': start_time + 9000,
            'url': f'https://www.chess.com/tournament/live/{SLUG}',
            'settings': {'type': 'swiss', 'rules': rules}}


_STANDINGS = [
    {'rank': 1, 'title': 'IM', 'username': 'Turboplombir', 'rating': 2714, 'score': 9.5, 'tie_break': 68.0,
     'aroc_1': 2630.0, 'performance_rating': 2920.9, 'wins': 9, 'draws': 1, 'byes': 0},
    {'rank': 2, 'title': 'GM', 'username': 'jefferyx', 'rating': 2743, 'score': 9.0, 'tie_break': 75.0,
     'aroc_1': 2646.0, 'performance_rating': 2900.5, 'wins': 8, 'draws': 2, 'byes': 0},
]


def test_process_writes_the_event_and_replaces_on_rerun(database, monkeypatch):
    monkeypatch.setattr(uff, 'fetch_standings', lambda slug, headers=None: list(_STANDINGS))
    session = _Session({uff.API.format(slug=SLUG): _details()})
    uff.process(session, SLUG, date(2026, 11, 20))
    uff.process(session, SLUG, date(2026, 11, 20))
    with db.get_conn() as conn:
        rows = conn.execute('SELECT username, rank, score, tie_break, substr(CAST(date AS TEXT), 1, 10) '
                            'FROM freestyle_friday_standings ORDER BY rank').fetchall()
        tourn = conn.execute('SELECT num_players, winner FROM freestyle_friday_tournaments').fetchall()
    assert rows == [('Turboplombir', 1, 9.5, 68.0, '2026-11-20'), ('jefferyx', 2, 9.0, 75.0, '2026-11-20')]
    assert tourn == [(2, 'Turboplombir')]
    assert uff.latest_event_date() == date(2026, 11, 20)
    assert uff.missing_dates(date(2026, 12, 5)) == [date(2026, 11, 27), date(2026, 12, 4)]


@pytest.mark.parametrize('details, error', [
    (_details(rules='chess'), ValueError),
    (_details(status='in_progress'), RuntimeError),
    (_details(start_time=START - 86400), ValueError),     # a Thursday
])
def test_process_rejects_the_wrong_event(database, monkeypatch, details, error):
    monkeypatch.setattr(uff, 'fetch_standings', lambda slug, headers=None: list(_STANDINGS))
    with pytest.raises(error):
        uff.process(_Session({uff.API.format(slug=SLUG): details}), SLUG)


def test_gaps_lists_missing_fridays(database):
    with db.get_conn() as conn:
        for day in ('2026-11-06', '2026-11-20'):
            conn.execute('INSERT INTO freestyle_friday_tournaments (tournament_slug, date) VALUES (?, ?)',
                         (f'freestyle-friday-{day}', f'{day} 00:00:00+00:00'))
    assert uff.gaps() == ['2026-11-13']


def test_run_is_a_no_op_when_the_latest_event_is_stored(database, monkeypatch):
    monkeypatch.setattr(uff, 'missing_dates', lambda today=None: [])
    monkeypatch.setattr(uff, 'discover_slugs', lambda *a: pytest.fail('should not search archives'))
    result = uff.run()
    assert result['mode'] == 'up_to_date' and result['events'] == []
