"""Attendance researcher, phase 1: names, clash test, Lichess collector, store, runs, queue lane.

No network: the Lichess collector runs against a fake client. Runs on SQLite, and
on Postgres too when TEST_PG_URL is set (tables are dropped).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src import db, job_queue as q, strategies
from src.markets import BULLET_BRAWL, TITLED_TUESDAY
from src.research import lichess, store
from src.research.candidates import load_candidates
from src.research.config import ResearchParams
from src.research.names import hint_match, match_grade, normalize
from src.research.operator import OperatorEvent
from src.research.overlap import classify, event_window, find_leads
from src.research.run import run
from tests.test_trade_jobs import MARKET, backend  # noqa: F401

EVENT_DATE = '2026-11-24'          # a Tuesday; Titled Tuesday starts 11:00 ET = 16:00 UTC
P = ResearchParams()


@pytest.fixture
def rdb(backend, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, '_ENGINE', None)        # the engine is cached per process; point it at this DB
    with db.get_conn() as conn:
        if backend == 'postgres':
            for t in ('research_runs', 'research_events', 'research_control',
                      'player_information', 'latest_model_predictions_raw'):
                conn.execute(f'DROP TABLE IF EXISTS {t}')
        conn.execute('CREATE TABLE player_information (username TEXT PRIMARY KEY, player_name TEXT)')
        conn.executemany('INSERT INTO player_information VALUES (?, ?)', [
            ('nihalsarin', 'Nihal Sarin'), ('Hikaru', 'Hikaru Nakamura'),
            ('HikaruAlt', 'Hikaru Nakamura'), ('rare', 'Rare Player'), ('weak', 'Weak Player'),
        ])
        conn.execute('CREATE TABLE latest_model_predictions_raw (username TEXT, p_participate REAL, '
                     '"P_top1_given_play" REAL, "P_top3_given_play" REAL, "P_top5_given_play" REAL, '
                     '"P_top8_given_play" REAL, "P_top10_given_play" REAL, tourn_date TEXT, market TEXT)')
        conn.executemany('INSERT INTO latest_model_predictions_raw VALUES (?, ?, 0, 0, 0, ?, 0, ?, ?)', [
            ('nihalsarin', 0.9, 0.30, EVENT_DATE, MARKET),     # fair 27%
            ('Hikaru', 0.8, 0.50, EVENT_DATE, MARKET),         # fair 40%
            ('HikaruAlt', 0.1, 0.50, EVENT_DATE, MARKET),      # same player, 5%: dropped as a duplicate
            ('rare', 0.05, 0.60, EVENT_DATE, MARKET),          # strong but rarely plays: 3%
            ('weak', 0.9, 0.02, EVENT_DATE, MARKET),           # always plays, rarely top 8: 1.8%
        ])
    store.init_schema()
    market = strategies.MARKETS[MARKET]
    original = market.next_event_date
    object.__setattr__(market, 'next_event_date', lambda: EVENT_DATE)
    yield backend
    object.__setattr__(market, 'next_event_date', original)
    db._ENGINE = None


def ev(name='Event', rounds=(), players=(), hint=None, tier=3, fide_tc='standard', start=EVENT_DATE,
       end=None, source='lichess', **kw):
    return {'id': kw.get('id'), 'source': source, 'source_id': kw.get('source_id', name), 'name': name,
            'url': None, 'tier': tier, 'fide_tc': fide_tc, 'time_zone': 'UTC', 'location': None,
            'start_date': start, 'end_date': end or start,
            'rounds': [{'name': f'R{i + 1}', 'starts_at': s, 'finished': False} for i, s in enumerate(rounds)],
            'players': [{'name': p} for p in players], 'players_hint': hint,
            'policy': kw.get('policy', 'auto'), 'policy_value': None}


# ── Names ────────────────────────────────────────────────────────────────────

def test_normalize_and_grades():
    assert normalize('Sarin, Nihal') == 'nihal sarin'
    assert normalize('GM Ján Markoš (SVK)') == 'jan markos'
    assert match_grade('Nihal Sarin', 'Sarin, Nihal') == 'exact'
    assert match_grade('Alireza Firouzja', 'Firouzja, Alireza Jr') == 'strong'
    assert match_grade('Parham Maghsoodloo', 'Maghsoodloo, P.') == 'partial'
    # A shared given name never matches on its own (seen live: Deac matched six events via "Daniel").
    assert match_grade('Alexander Grischuk', 'Smith, Alexander') is None
    assert match_grade('Bogdan-Daniel Deac', 'Popescu, Daniel') is None
    assert match_grade('Jan-Krzysztof Duda', 'Nowak, Krzysztof') is None
    # Surname match needs a compatible first initial.
    assert match_grade('Pranav Venkatesh', 'Venkatesh, M. R.') is None
    assert match_grade('Pranav Venkatesh', 'Venkatesh, P.') == 'partial'
    assert match_grade('Nihal Sarin', 'Carlsen, Magnus') is None


def test_hint_match():
    assert hint_match('Nihal Sarin', 'Nihal, Maghsoodloo, Yakubboev')
    assert hint_match('Gukesh D', 'Gukesh Dommaraju vs Javokhir Sindarov')
    assert not hint_match('Magnus Carlsen', 'Nihal, Maghsoodloo, Yakubboev')
    assert not hint_match('Wei Yi', 'Nihal, Maghsoodloo')          # short words never hint


# ── Clash test ───────────────────────────────────────────────────────────────

def test_window_uses_spec_start_and_duration():
    w = event_window(TITLED_TUESDAY, EVENT_DATE)
    assert (w.start.hour, w.end.hour, w.end.minute) == (16, 18, 30)       # 11:00–13:30 ET in November
    assert event_window(BULLET_BRAWL, '2026-11-28').end.hour == 19         # 12:00 ET + 2 h


def test_classify_hard_adjacent_multi_day():
    w = event_window(TITLED_TUESDAY, EVENT_DATE)
    hard = classify(ev(rounds=['2026-11-24T15:00:00+00:00']), w, P)       # classical 15:00–20:00 UTC
    assert hard['kind'] == 'hard' and hard['rounds'][0]['overlap_minutes'] == 150
    adj = classify(ev(rounds=['2026-11-24T10:00:00+00:00']), w, P)        # ends 15:00, 1 h before
    assert adj['kind'] == 'adjacent' and adj['rounds'][0]['gap_minutes'] == 60
    rapid = classify(ev(rounds=['2026-11-24T19:00:00+00:00'], fide_tc='rapid'), w, P)
    assert rapid['kind'] == 'adjacent'
    rest_day = classify(ev(rounds=['2026-11-23T15:00:00+00:00', '2026-11-25T15:00:00+00:00'],
                           start='2026-11-23', end='2026-11-25'), w, P)
    assert rest_day['kind'] == 'multi_day' and {r['clash'] for r in rest_day['rounds']} == {'mid_event'}
    assert classify(ev(rounds=['2026-11-22T10:00:00+00:00'], start='2026-11-22'), w, P) is None


def test_season_long_league_only_counts_through_its_rounds():
    # Seen live: the Bundesliga spans October to April, so every Tuesday "spanned" it.
    w = event_window(TITLED_TUESDAY, EVENT_DATE)
    league = ev('Bundesliga', rounds=['2026-11-21T13:00:00+00:00', '2026-12-05T13:00:00+00:00'],
                start='2026-10-03', end='2027-04-25')
    assert classify(league, w, P) is None
    assert classify(ev('Short, untimed', start='2026-11-22', end='2026-11-26'), w, P)['kind'] == 'multi_day'
    assert classify(ev('Long, untimed', start='2026-10-01', end='2027-04-01'), w, P) is None


def test_find_leads_matches_players_and_flags_unknown_elite_fields():
    w = event_window(TITLED_TUESDAY, EVENT_DATE)
    candidates = [{'player_name': 'Hikaru Nakamura', 'username': 'Hikaru', 'fair_value': 0.4, 'p_participate': 0.8},
                  {'player_name': 'Nihal Sarin', 'username': 'nihalsarin', 'fair_value': 0.27, 'p_participate': 0.9}]
    events = [
        ev('Spanish Club Teams', rounds=['2026-11-24T15:00:00+00:00'], players=['Sarin, Nihal', 'Eljanov, Pavel']),
        ev('Minor Open', rounds=['2026-11-24T10:00:00+00:00'], hint='Nakamura, Somebody'),
        ev('Elite RR, pairings later', rounds=['2026-11-24T16:00:00+00:00'], tier=5),
        ev('Minor, players unknown', rounds=['2026-11-24T16:00:00+00:00'], tier=3),
        ev('Youth champs', rounds=['2026-11-24T16:00:00+00:00'], tier=4),
        ev('Title match', rounds=['2026-11-24T14:00:00+00:00'], tier=5, hint='Gukesh Dommaraju vs Javokhir Sindarov'),
    ]
    leads = find_leads(candidates, events, w, P)
    assert [(l['kind'], l['player_name'], l['match']) for l in leads] == [
        ('hard', 'Nihal Sarin', 'exact'),
        ('hard', None, None),                           # elite, no players yet
        ('adjacent', 'Hikaru Nakamura', 'hint'),
    ]


# ── Lichess collector ────────────────────────────────────────────────────────

class FakeLichess:
    def __init__(self, fail_tour=None):
        self.requests = 0
        self.tour_calls = []
        self.fail_tour = fail_tour

    def top(self):
        self.requests += 1
        return [
            {'tour': {'id': 'near1', 'name': 'Near Open', 'tier': 3, 'dates': [1795532400000],   # 2026-11-24
                      'info': {'fideTC': 'standard', 'players': 'Sarin', 'timeZone': 'Europe/Madrid'}},
             'round': {'id': 'r'}},
            {'tour': {'id': 'far', 'name': 'Far Away', 'tier': 5, 'dates': [1798470000000]}},     # late Dec
        ]

    def official(self):
        self.requests += 1
        return [{'tour': {'id': 'elite', 'name': 'Elite RR', 'tier': 5,
                          'dates': [1795446000000, 1795878000000], 'info': {'fideTC': 'standard'}},
                 'rounds': [{'name': 'Round 2', 'startsAt': 1795532400000, 'finished': False}]}]

    def tour(self, tid):
        self.requests += 1
        self.tour_calls.append(tid)
        if tid == self.fail_tour:
            import requests
            raise requests.ConnectionError('boom')
        return {'tour': {'id': tid}, 'rounds': [{'name': 'Round 1', 'startsAt': 1795532400000}]}

    def players(self, tid):
        self.requests += 1
        return [{'name': 'Sarin, Nihal', 'fideId': 25092340, 'title': 'GM', 'fed': 'IND'}] if tid == 'near1' else []


def test_collect_keeps_near_events_of_every_tier_and_fetches_rounds_only_when_missing():
    fake = FakeLichess()
    events, stats = lichess.collect(date(2026, 11, 24), P, fake, log=lambda m: None)
    by_id = {e['source_id']: e for e in events}
    assert set(by_id) == {'near1', 'elite'}               # 'far' is out of range; tier 3 kept
    assert fake.tour_calls == ['near1']                   # official listing already had rounds
    near = by_id['near1']
    assert near['players'][0]['fide_id'] == 25092340 and near['time_zone'] == 'Europe/Madrid'
    assert near['rounds'][0]['starts_at'].startswith('2026-11-24T')
    assert by_id['elite']['start_date'] == '2026-11-23' and by_id['elite']['end_date'] == '2026-11-28'
    assert stats['collected'] == 2 and stats['failed'] == 0


def test_collect_survives_one_failing_tour():
    events, stats = lichess.collect(date(2026, 11, 24), P, FakeLichess(fail_tour='near1'), log=lambda m: None)
    assert [e['source_id'] for e in events] == ['elite'] and stats['failed'] == 1


# ── Store, candidates, operator events ──────────────────────────────────────

def test_upsert_keeps_operator_policy(rdb):
    eid = store.upsert_event(ev('Bundesliga', source_id='bl'))
    store.set_policy(eid, 'nudge_only', -0.5, 'they draw quickly')
    again = store.upsert_event({**ev('Bundesliga 2026/27', source_id='bl')})
    got = store.get_event(eid)
    assert again == eid and got['name'] == 'Bundesliga 2026/27'
    assert (got['policy'], got['policy_value'], got['policy_note']) == ('nudge_only', -0.5, 'they draw quickly')
    with pytest.raises(ValueError):
        store.set_policy(eid, 'override', 1.5)
    with pytest.raises(ValueError):
        store.set_policy(eid, 'nudge_only')


def test_events_overlapping_and_operator_add_delete(rdb):
    store.upsert_event(ev('Before', start='2026-11-20', end='2026-11-22', source_id='a'))
    store.upsert_event(ev('Spanning', start='2026-11-20', end='2026-11-26', source_id='b'))
    op = OperatorEvent(name='Chess.com event', start_date=date(2026, 11, 24), round_time='17:00',
                       time_zone='Europe/Madrid', players=['Hikaru Nakamura'])
    oid = store.add_operator_event(op.to_record())
    names = [e['name'] for e in store.events_overlapping('2026-11-23', '2026-11-25')]
    assert names == ['Spanning', 'Chess.com event']
    assert store.get_event(oid)['rounds'][0]['starts_at'] == '2026-11-24T16:00:00+00:00'   # CET → UTC
    assert store.delete_operator_event(oid) and not store.delete_operator_event(oid)


def test_operator_event_validation():
    with pytest.raises(ValueError):
        OperatorEvent(name='X', start_date=date(2026, 11, 24), time_zone='Mars/Base')
    with pytest.raises(ValueError):
        OperatorEvent(name='X', start_date=date(2026, 11, 24), end_date=date(2026, 11, 20))


def test_candidates_use_raw_top8_fair_value(rdb):
    cands = load_candidates(TITLED_TUESDAY, P)
    assert [c['player_name'] for c in cands] == ['Hikaru Nakamura', 'Nihal Sarin']
    assert cands[0]['username'] == 'Hikaru' and cands[0]['fair_value'] == pytest.approx(0.4)
    with pytest.raises(ValueError):
        load_candidates(TITLED_TUESDAY, ResearchParams(candidate_n=4))


# ── Runs ─────────────────────────────────────────────────────────────────────

def test_run_end_to_end(rdb):
    store.add_operator_event(OperatorEvent(name='Pasted event', start_date=date(2026, 11, 24),
                                           round_time='11:30', time_zone='America/New_York',
                                           players=['Hikaru Nakamura']).to_record())
    result = run(TITLED_TUESDAY, EVENT_DATE, lichess_client=FakeLichess(), log=lambda m: None)
    saved = store.latest_run(MARKET)
    assert result['status'] == saved['status'] == 'leads'
    got = {(l['player_name'], l['event_name'], l['kind']) for l in saved['leads']}
    assert ('Nihal Sarin', 'Near Open', 'hard') in got
    assert ('Hikaru Nakamura', 'Pasted event', 'hard') in got
    assert saved['stats']['operator_events'] == 1 and saved['collector_errors'] == {}


def test_run_fails_when_every_collector_fails(rdb):
    class Down(FakeLichess):
        def top(self):
            raise ConnectionError('lichess down')
    result = run(TITLED_TUESDAY, EVENT_DATE, lichess_client=Down(), log=lambda m: None)
    saved = store.latest_run(MARKET)
    assert result['status'] == saved['status'] == 'failed' and 'lichess down' in saved['error']
    assert strategies._research_error(TITLED_TUESDAY).startswith('Every collector failed')


# ── Queue lane ───────────────────────────────────────────────────────────────

def test_research_runs_in_background_without_blocking_trades(rdb):
    rid = strategies.submit_ops(MARKET, 'research')
    assert q.get_job(rid)['lane'] == 'background'
    assert q.claim_next('w', 'normal') is None
    assert q.claim_next('w', 'background')['id'] == rid            # research running…
    nid = q.enqueue(market=MARKET, strategy='cancel_bids', tourn_date=EVENT_DATE, params={})
    assert q.claim_next('w', 'normal')['id'] == nid                # …and the trade lane is free
    q.enqueue_kill(MARKET, EVENT_DATE)
    assert q.get_job(rid)['cancel_requested'] is False             # kill leaves research alone
    with pytest.raises(strategies.SubmissionError):
        strategies.submit_ops(MARKET, 'research')                  # one at a time


def test_research_waits_behind_nothing(rdb):
    nid = q.enqueue(market=MARKET, strategy='cancel_bids', tourn_date=EVENT_DATE, params={})
    assert q.claim_next('w', 'normal')['id'] == nid
    rid = q.enqueue(market=MARKET, strategy='research', tourn_date=EVENT_DATE, params={}, lane='background')
    assert q.claim_next('w', 'background')['id'] == rid            # a running trade doesn't block it


def test_migration_adds_background_lane_to_old_sqlite_table(tmp_path, monkeypatch):
    import scripts.migrate_research as mig
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'old.db')
    with db.get_conn() as conn:
        for stmt in q._ddl():
            conn.execute(stmt.replace("'normal', 'priority', 'background'", "'normal','priority'"))
        conn.execute("INSERT INTO trade_jobs (market, strategy, tourn_date, params, status, run_at, created_at) "
                     "VALUES ('m', 'cancel_bids', 'd', '{}', 'succeeded', 'x', 'x')")
        conn.execute("INSERT INTO trade_job_events (job_id, ts, kind, message) VALUES (1, 'x', 'k', 'm')")
    with pytest.raises(Exception):
        q.enqueue(market='m', strategy='research', tourn_date='d', params={}, lane='background')
    assert mig.migrate() == 'rebuilt trade_jobs with the new lane check'
    assert mig.migrate() == 'already allows background'
    q.enqueue(market='m', strategy='research', tourn_date='d', params={}, lane='background')
    with db.get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM trade_jobs').fetchone()[0] == 2
        fk = conn.execute("SELECT sql FROM sqlite_master WHERE name='trade_job_events'").fetchone()[0]
        assert 'REFERENCES trade_jobs(id)' in fk
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='idx_trade_jobs_one_running'").fetchone()


# ── API ──────────────────────────────────────────────────────────────────────

from tests.test_api import PASSWORD, authed, client  # noqa: E402,F401


def test_api_research_overview_run_events_and_policy(rdb, authed):  # noqa: F811
    # The client fixture pins its own event date; this test is about EVENT_DATE.
    object.__setattr__(strategies.MARKETS[MARKET], 'next_event_date', lambda: EVENT_DATE)
    store.upsert_event(ev('Clashing RR', rounds=['2026-11-24T15:00:00+00:00'], players=['Sarin, Nihal'],
                          source_id='rr', tier=4))
    run(TITLED_TUESDAY, EVENT_DATE, lichess_client=FakeLichess(), log=lambda m: None)

    added = authed.post('/api/research/events', json={
        'name': 'From chess.com', 'start_date': EVENT_DATE, 'round_time': '10:00',
        'time_zone': 'America/New_York', 'players': ['Hikaru Nakamura']})
    assert added.status_code == 200
    bad = authed.post('/api/research/events', json={'name': 'X', 'start_date': EVENT_DATE, 'time_zone': 'Nowhere'})
    assert bad.status_code == 422

    body = authed.get(f'/api/markets/{MARKET}/research').json()
    assert body['run']['status'] == 'leads' and body['run']['candidate_count'] == 2
    assert 'candidates' not in body['run']
    first = body['events'][0]
    assert first['clash']['kind'] == 'hard' and 'players' not in first and first['player_count'] >= 0
    op = next(e for e in body['events'] if e['source'] == 'operator')
    assert op['players'] == ['Hikaru Nakamura']

    eid = next(e['id'] for e in body['events'] if e['name'] == 'Clashing RR')
    assert authed.put(f'/api/research/events/{eid}/policy', json={'policy': 'nudge_only'}).status_code == 422
    assert authed.put(f'/api/research/events/{eid}/policy',
                      json={'policy': 'nudge_only', 'value': -0.5}).status_code == 200
    assert store.get_event(eid)['policy_value'] == -0.5
    assert authed.delete(f'/api/research/events/{eid}').status_code == 404      # only operator events
    assert authed.delete(f'/api/research/events/{added.json()["id"]}').status_code == 200

    job = authed.post(f'/api/markets/{MARKET}/research/run').json()
    assert q.get_job(job['id'])['lane'] == 'background'


def test_api_research_requires_login(client):  # noqa: F811
    assert client.get(f'/api/markets/{MARKET}/research').status_code == 401
    assert client.post('/api/research/events', json={}).status_code == 401
