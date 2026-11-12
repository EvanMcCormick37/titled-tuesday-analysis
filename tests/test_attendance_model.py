"""Production p_participate must include the season shift."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import data, db
from src.data import load_and_prepare
from src.markets import BULLET_BRAWL, TITLED_TUESDAY

EVENTS = pd.date_range('2026-06-02', '2026-11-03', freq='7D')
POST_SHIFT = EVENTS[EVENTS >= pd.Timestamp(TITLED_TUESDAY.model.season_shift_date)]
PRE_SHIFT = EVENTS[EVENTS < pd.Timestamp(TITLED_TUESDAY.model.season_shift_date)]
_PRED_COLS = ', '.join(f'"P_top{n}_given_play" REAL' for n in (1, 3, 5, 8, 10))


def _rows(username: str, dates) -> list[dict]:
    return [{'date': f'{d.date()} 00:00:00+00:00', 'tournament_slug': f's{d.date()}', 'rank': 1.0,
             'username': username, 'score': 8.0, 'tie_break': 50.0} for d in dates]


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(db, '_ENGINE', None)
    monkeypatch.setattr(data, '_USERNAME_TO_PLAYER', None)
    monkeypatch.setattr(data, '_PLAYER_TO_USERNAME', None)
    standings = pd.DataFrame(_rows('always', EVENTS) + _rows('post', POST_SHIFT) + _rows('pre', PRE_SHIFT))
    with db.get_conn() as conn:
        conn.execute('CREATE TABLE player_information (username TEXT PRIMARY KEY, player_name TEXT, is_default INTEGER)')
        conn.execute("CREATE TABLE attendance_conflicts (market TEXT, date TEXT, player_name TEXT, username TEXT)")
        conn.execute(f"CREATE TABLE latest_model_predictions_raw (market TEXT, username TEXT, {_PRED_COLS})")
    standings.to_sql('titled_tuesday_standings', db.get_engine(), index=False)
    yield
    db.get_engine().dispose()


def _shift_weights(as_of: str, dates) -> np.ndarray:
    model = TITLED_TUESDAY.model
    k = np.array([(pd.Timestamp(as_of) - d) // pd.Timedelta(weeks=1) for d in dates])
    shift = np.where(dates < pd.Timestamp(model.season_shift_date), model.season_shift_factor, 1.0)
    return model.participation_decay ** k * shift


def test_bullet_brawl_zero_game_rows_do_not_count_as_attending():
    dates = pd.date_range('2026-09-05', '2026-10-31', freq='7D')
    rows = [{'date': f'{d.date()} 00:00:00+00:00', 'tournament_slug': f'b{d.date()}', 'rank': r, 'username': u,
             'score': 100.0 if games else 0.0, 'tie_break': 0.0, 'games': games}
            for d in dates
            for r, (u, games) in enumerate([('always', 20), ('registered', 0)], 1)]
    rows += [{**rows[0], 'username': 'once', 'rank': 3.0}]
    pd.DataFrame(rows).to_sql('bullet_brawl_standings', db.get_engine(), index=False)

    df, p_participate, _ = load_and_prepare(as_of='2026-11-07', spec=BULLET_BRAWL)

    assert 'registered' not in set(df['username'])
    assert 'registered' not in p_participate.index
    assert p_participate['always'] == pytest.approx(1.0)
    assert p_participate['once'] < 0.2


def test_season_shift_date():
    assert pd.Timestamp(TITLED_TUESDAY.model.season_shift_date) == pd.Timestamp('2026-08-31')
    assert PRE_SHIFT[-1] == pd.Timestamp('2026-08-25') and POST_SHIFT[0] == pd.Timestamp('2026-09-01')


@pytest.mark.parametrize('as_of', ['2026-11-04', '2026-11-08', '2026-11-11 08:00'])
def test_perfect_attendance_stays_one_however_late_in_the_week_it_runs(as_of):
    _, p, _ = load_and_prepare(as_of=as_of)
    assert p['always'] == pytest.approx(1.0)


def test_season_shift_discounts_older_events():
    as_of = '2026-11-04'
    _, p, _ = load_and_prepare(as_of=as_of)
    post, pre = _shift_weights(as_of, POST_SHIFT).sum(), _shift_weights(as_of, PRE_SHIFT).sum()
    assert p['post'] == pytest.approx(post / (post + pre))


def test_low_attendance_is_not_floored():
    as_of = '2026-11-04'
    _, p, _ = load_and_prepare(as_of=as_of)
    post, pre = _shift_weights(as_of, POST_SHIFT).sum(), _shift_weights(as_of, PRE_SHIFT).sum()
    raw = pre / (post + pre)
    assert raw < 0.05 and p['pre'] == pytest.approx(raw)


def test_attendance_window_excludes_the_shift():
    as_of = '2026-11-04'
    _, p, _ = load_and_prepare(as_of=as_of, attendance_start='2026-06-02')
    weights = TITLED_TUESDAY.model.participation_decay ** np.arange(len(EVENTS))
    pre_mask = np.array([d < pd.Timestamp(TITLED_TUESDAY.model.season_shift_date) for d in EVENTS[::-1]])
    assert p['pre'] == pytest.approx(weights[pre_mask].sum() / weights.sum())


@pytest.mark.parametrize('as_of', ['2026-11-03', '2026-11-04', '2026-11-11 08:00'])
def test_attendance_window_is_not_deflated_when_the_latest_event_is_a_week_old(as_of):
    _, p, _ = load_and_prepare(as_of=as_of, attendance_start='2026-06-02')
    assert p['always'] == pytest.approx(1.0)
