"""Bullet Brawl standings parsing and storage."""
from __future__ import annotations

import pytest

from src import bullet_brawl_ingest as ingest, db
from src.multi_market_schema import migrate

SLUG, DATE = 'bullet-brawl-november-07-2026-1', '2026-11-07'


def _game(end, white, white_result, black, black_result):
    return {'end_time': end, 'white': {'username': white, 'result': white_result, 'rating': 3000},
            'black': {'username': black, 'result': black_result, 'rating': 2900}}


PAYLOAD = {
    'games': [
        _game(1, 'Hero', 'win', 'Foe', 'resigned'),
        _game(2, 'Foe', 'timeout', 'Hero', 'win'),
        _game(3, 'Hero', 'win', 'Foe', 'checkmated'),
        _game(4, 'Foe', 'agreed', 'Hero', 'agreed'),
        _game(5, 'Hero', 'win', 'Foe', 'resigned'),
    ],
    'players': [{'username': 'hero', 'points': 12, 'place_finish': 1},
                {'username': 'foe', 'points': 1, 'place_finish': 2},
                {'username': 'noshow', 'points': 0, 'place_finish': 3}],
}


def _rows():
    return {r['username'].lower(): r for r in ingest.parse_round(SLUG, DATE, PAYLOAD).rows}


def test_totals_and_streak_scoring():
    parsed = ingest.parse_round(SLUG, DATE, PAYLOAD)
    assert parsed.points_mismatches == 0   # 2 + 3 + 4 + draw 1 + 2 = 12
    hero = _rows()['hero']
    assert (hero['wins'], hero['draws'], hero['losses'], hero['games']) == (4, 1, 0, 5)
    assert hero['longest_streak'] == 3 and hero['display_name'] == hero['username'] == 'Hero'
    assert hero['rank'] == 1 and hero['date'] == '2026-11-07 00:00:00+00:00'


def test_points_mismatch_is_counted():
    payload = {**PAYLOAD, 'players': [{'username': 'hero', 'points': 99, 'place_finish': 1}]}
    assert ingest.parse_round(SLUG, DATE, payload).points_mismatches == 1


def test_player_without_games_is_kept():
    noshow = _rows()['noshow']
    assert noshow['games'] == 0 and noshow['score'] == 0 and noshow['first_game_ts'] is None


def test_tie_break_orders_streak_then_wins_then_losses():
    assert ingest.tie_break_value(5, 10, 3) > ingest.tie_break_value(4, 40, 0)
    assert ingest.tie_break_value(5, 11, 9) > ingest.tie_break_value(5, 10, 0)
    assert ingest.tie_break_value(5, 10, 1) > ingest.tie_break_value(5, 10, 2)


def test_write_event_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    with db.get_conn() as conn:
        migrate(conn)
    parsed = ingest.parse_round(SLUG, DATE, PAYLOAD)
    details = {'start_time': 10, 'finish_time': 20, 'url': 'u'}
    for _ in range(2):
        with db.get_conn() as conn:
            ingest.write_event(conn, SLUG, DATE, details, parsed)
    with db.get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bullet_brawl_standings').fetchone()[0] == 3
        row = conn.execute('SELECT num_players, num_games, winner FROM bullet_brawl_tournaments').fetchone()
    assert row == (3, 5, 'Hero')


_PAGE = """
<div data-total-pages="196"></div><div data-total-pages="10"></div>
<table><tr>
  <td class="tournaments-live-view-results-user-cell">#1 GM
    <a v-user-popover='{"username":"DanielNaroditsky","location":"live_tournament"}'>DanielNaroditsky</a> (3153)</td>
  <td class="tournament-score"><div class="tournaments-live-view-total-score" v-tooltip="68 wins">
    246</div><div>24 / 68 W / 1 D / 12 L</div></td>
</tr></table>"""


def test_standings_page_parsing():
    row = ingest.parse_standings_page(_PAGE, SLUG, DATE)[0]
    assert row['username'] == row['display_name'] == 'DanielNaroditsky'
    assert (row['rank'], row['score'], row['longest_streak']) == (1, 246, 24)
    assert (row['wins'], row['draws'], row['losses'], row['games']) == (68, 1, 12, 81)
    assert ingest.player_page_count(_PAGE) == 10


def test_get_json_returns_none_on_404():
    class Response:
        status_code = 404

    class Session:
        def get(self, url, timeout):
            return Response()

    assert ingest.get_json(Session(), 'x') is None


@pytest.mark.parametrize('result', sorted(ingest.DRAW_RESULTS))
def test_every_draw_result_scores_one(result):
    payload = {'games': [_game(1, 'A', result, 'B', result)],
               'players': [{'username': 'a', 'points': 1, 'place_finish': 1}]}
    parsed = ingest.parse_round(SLUG, DATE, payload)
    assert parsed.points_mismatches == 0 and parsed.rows[0]['draws'] == 1


def test_write_event_adopts_the_recorded_capitalization(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    with db.get_conn() as conn:
        migrate(conn)
        conn.execute('CREATE TABLE player_information (username TEXT, player_name TEXT)')
        conn.execute('CREATE TABLE titled_tuesday_standings (username TEXT, date TEXT)')
        conn.executemany('INSERT INTO player_information VALUES (?, ?)',
                         [('foe', 'A Foe'), ('FOE', 'A Foe'), ('noshow', None), ('NoShow', None)])
        conn.execute("INSERT INTO titled_tuesday_standings VALUES ('HERO', '2026-11-03')")
    with db.get_conn() as conn:
        ingest.write_event(conn, SLUG, DATE, {}, ingest.parse_round(SLUG, DATE, PAYLOAD))
    with db.get_conn() as conn:
        names = {r[0] for r in conn.execute('SELECT username FROM bullet_brawl_standings').fetchall()}
        winner = conn.execute('SELECT winner FROM bullet_brawl_tournaments').fetchone()[0]
    # TT's spelling wins; a capitalized player_information spelling beats a lowercase copy.
    assert names == {'HERO', 'FOE', 'NoShow'} and winner == 'HERO'
