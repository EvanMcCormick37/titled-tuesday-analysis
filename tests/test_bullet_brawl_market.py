"""Bullet Brawl as a traded market: weekly discovery, name matching, pipeline wiring."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

from src import bullet_brawl_ingest as ingest, data, db
from src.markets import BULLET_BRAWL, TITLED_TUESDAY
from src.multi_market_schema import migrate

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts' / 'scraping')]
import update_bullet_brawl as ubb  # noqa: E402

SLUG = 'bullet-brawl-november-14-2026-31411111'


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


# ── Slug helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    SLUG,
    f'https://www.chess.com/tournament/live/arena/{SLUG}',
    f'https://www.chess.com/tournament/live/{SLUG}?&players=2',
    f'https://api.chess.com/pub/tournament/{SLUG}',
])
def test_slug_from_url(text):
    assert ingest.slug_from_url(text) == SLUG


def test_slug_from_url_rejects_other_events():
    with pytest.raises(ValueError):
        ingest.slug_from_url('https://www.chess.com/tournament/live/titled-tuesday-blitz-november-10-2026-1')


def test_slug_date():
    assert ingest.slug_date(SLUG) == date(2026, 11, 14)
    assert ingest.slug_date('bullet-brawl-smarch-14-2026-31411111') is None


def test_archive_scan_ignores_community_and_other_arenas():
    archive = {'games': [
        {'tournament': f'https://api.chess.com/pub/tournament/{SLUG}'},
        {'tournament': 'https://api.chess.com/pub/tournament/community-bullet-brawl-november-14-2026-31411112'},
        {'tournament': 'https://api.chess.com/pub/tournament/titled-tuesday-blitz-november-10-2026-31221249'},
        {'url': 'no tournament'},
    ]}
    assert ingest.slugs_in_archive(archive) == {SLUG}
    assert ingest.slugs_in_archive(None) == set()


def test_event_day_is_eastern():
    assert ingest.event_day(1794070800) == '2026-11-07'     # 12:00 EST
    assert ingest.event_day(None) is None


# ── Name matching ────────────────────────────────────────────────────────────

def test_match_player_exact_then_unambiguous_word_set():
    names = {'Renato Terry Celis': 'terry', 'Hikaru Nakamura': 'hikaru', 'Jose Martinez Alcantara': 'jospem',
             'Ivan Petrov': 'ip1', 'Ivan Petrov Sidorov': 'ip2'}
    assert data.match_player('Hikaru Nakamura', names) == 'hikaru'
    assert data.match_player('Renato Terry', names) == 'terry'             # Kalshi drops a surname
    assert data.match_player('Hikaru Nakamura Jr.', names) == 'hikaru'     # Kalshi adds a word
    assert data.match_player('Ivan Petrov', names) == 'ip1'                # exact beats fuzzy
    assert data.match_player('Petrov Ivan', names) is None                 # matches both Ivans
    assert data.match_player('Hikaru', names) is None                      # one word is too weak
    assert data.match_player('Nobody Atall', names) is None


def test_market_map_uses_the_account_each_market_sees(database):
    with db.get_conn() as conn:
        conn.executemany('INSERT INTO player_information VALUES (?, ?, ?)', [
            ('main', 'Some Player', 1), ('bulletalt', 'Some Player', 0), ('other', 'Other Player', 1),
        ])
        conn.execute("INSERT INTO titled_tuesday_standings VALUES ('t', '2026-11-03', 'main', 1)")
        conn.executemany(
            'INSERT INTO bullet_brawl_standings (tournament_slug, date, username, games) VALUES (?, ?, ?, ?)',
            [('b1', '2026-10-31', 'main', 10), ('b2', '2026-11-07', 'bulletalt', 10)])
    pool = ['main', 'bulletalt', 'nameless']
    assert data.market_player_to_username(pool, BULLET_BRAWL)['Some Player'] == 'bulletalt'
    assert data.market_player_to_username(pool, TITLED_TUESDAY)['Some Player'] == 'main'
    bb = data.market_player_to_username(pool, BULLET_BRAWL)
    assert 'Other Player' not in bb                 # not in the pool
    assert bb['nameless'] == 'nameless'             # no display name: matched by username


# ── Weekly scrape ────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, body):
        self.status_code = 200 if body is not None else 404
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.headers = {}

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        return _Response(self.routes.get(url))


def _seed_event(slug: str, day: str, players: list[str]) -> None:
    with db.get_conn() as conn:
        conn.execute('INSERT INTO bullet_brawl_tournaments (tournament_slug, date) VALUES (?, ?)', (slug, day))
        conn.executemany(
            'INSERT INTO bullet_brawl_standings (tournament_slug, date, username, rank, games) VALUES (?, ?, ?, ?, ?)',
            [(slug, day, u, i + 1, 10) for i, u in enumerate(players)])


def _details(start_time=1794675600, status='finished', time_control='60'):
    return {'start_time': start_time, 'finish_time': start_time + 7200, 'status': status,
            'url': f'https://www.chess.com/tournament/live/arena/{SLUG}',
            'settings': {'time_control': time_control}}


_ROUND = {'games': [{'end_time': 1, 'white': {'username': 'Reg', 'result': 'win', 'rating': 3000},
                     'black': {'username': 'Other', 'result': 'resigned', 'rating': 2900}}],
          'players': [{'username': 'reg', 'points': 2, 'place_finish': 1},
                      {'username': 'other', 'points': 0, 'place_finish': 2}]}


def test_missing_dates_counts_saturdays_since_the_last_event(database):
    _seed_event('bullet-brawl-october-31-2026-1', '2026-10-31 00:00:00+00:00', ['reg'])
    assert ubb.missing_dates(date(2026, 11, 16)) == [date(2026, 11, 7), date(2026, 11, 14)]
    assert ubb.missing_dates(date(2026, 11, 6)) == []


def test_discovery_finds_new_slug_in_a_regulars_archive(database, monkeypatch):
    monkeypatch.setattr(ubb, 'PAUSE_SECONDS', 0)
    _seed_event('bullet-brawl-november-07-2026-1', '2026-11-07 00:00:00+00:00', ['reg', 'casual'])
    _seed_event('bullet-brawl-october-31-2026-1', '2026-10-31 00:00:00+00:00', ['reg'])
    archive_url = ubb.ARCHIVE.format(username='reg', year=2026, month=11)
    session = _Session({archive_url: {'games': [
        {'tournament': 'https://api.chess.com/pub/tournament/bullet-brawl-november-07-2026-1'},   # known
        {'tournament': f'https://api.chess.com/pub/tournament/{SLUG}'},
    ]}})
    assert ubb.regulars()[0] == 'reg'
    assert ubb.discover_slugs(session, [date(2026, 11, 14)]) == {date(2026, 11, 14): SLUG}
    assert session.calls == [archive_url]       # stops once every date is found


def test_process_loads_a_finished_event(database, monkeypatch):
    monkeypatch.setattr(ubb, 'PAUSE_SECONDS', 0)
    session = _Session({ingest.API.format(slug=SLUG): _details(), f'{ingest.API.format(slug=SLUG)}/1': _ROUND})
    summary = ubb.process(session, SLUG, date(2026, 11, 14))
    assert summary['date'] == '2026-11-14' and summary['winner'] == 'Reg'   # chess.com's case
    with db.get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bullet_brawl_standings').fetchone()[0] == 2


@pytest.mark.parametrize('details, expected, message', [
    (_details(status='in_progress'), date(2026, 11, 14), 'not finished'),
    (_details(time_control='180'), date(2026, 11, 14), r'not 1\+0'),
    (_details(), date(2026, 11, 7), 'expected 2026-11-07'),
    (_details(start_time=1794589200), None, 'not a Saturday'),       # a Friday
])
def test_process_refuses_the_wrong_event(database, details, expected, message):
    session = _Session({ingest.API.format(slug=SLUG): details, f'{ingest.API.format(slug=SLUG)}/1': _ROUND})
    with pytest.raises(Exception, match=message):
        ubb.process(session, SLUG, expected)
    with db.get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bullet_brawl_standings').fetchone()[0] == 0


def test_run_raises_listing_missing_when_nothing_is_found(database, monkeypatch):
    monkeypatch.setattr(ubb, 'missing_dates', lambda: [date(2026, 11, 14)])
    monkeypatch.setattr(ubb, 'discover_slugs', lambda session, wanted: {})
    with pytest.raises(ubb.ListingMissingSlugError):
        ubb.run()


def test_enrichment_skips_accounts_known_under_another_case(database, monkeypatch):
    enriched = []
    monkeypatch.setattr(ubb, 'enrich_players', lambda conn, usernames: enriched.extend(usernames))
    with db.get_conn() as conn:
        conn.execute("INSERT INTO player_information VALUES ('Hikaru', 'Hikaru Nakamura', 1)")
    _seed_event('bullet-brawl-november-07-2026-1', '2026-11-07 00:00:00+00:00', ['hikaru', 'NewKid'])
    assert ubb.enrich_new_players(min_appearances=1) == 1 and enriched == ['NewKid']
    with db.get_conn() as conn:
        assert sorted(r[0] for r in conn.execute('SELECT username FROM player_information')) == ['Hikaru', 'NewKid']
