"""Backtest run history, scoring, and the Railway snapshot."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

from src import backtest_runs as br

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))


def _predictions(p_given: float = 0.5) -> pd.DataFrame:
    row = {f'P_top{n}_given_play': p_given for n in br.N_VALUES}
    return pd.DataFrame([
        {'tourn_date': '2026-11-03', 'model': 'score', 'username': 'a', 'p_participate': 1.0,
         **row, 'played': 1, 'actual_rank': 1.0},
        {'tourn_date': '2026-11-03', 'model': 'score', 'username': 'b', 'p_participate': 0.5,
         **row, 'played': 0, 'actual_rank': None},
    ])


@pytest.fixture
def store(tmp_path):
    return tmp_path / 'backtests.db'


def _run(store, predictions, notes=None, error=None) -> int:
    with br.connect(store) as conn:
        run_id = br.start_run(conn, 'titled_tuesday', ('score',), {'n_sims': 10}, notes)
        br.save_predictions(conn, run_id, predictions)
        br.record_event(conn, run_id)
        br.finish_run(conn, run_id, error)
    return run_id


def test_runs_do_not_overwrite_each_other(store):
    first, second = _run(store, _predictions(0.5)), _run(store, _predictions(0.9))
    loaded = br.load_predictions([first, second], store).groupby('run_id')['P_top1_given_play'].max()
    assert loaded.to_dict() == {first: 0.5, second: 0.9}


def test_run_records_its_settings_and_outcome(store):
    ok = _run(store, _predictions(), notes='baseline')
    failed = _run(store, _predictions(), error='boom')
    runs = br.list_runs(store)
    assert runs.loc[ok, 'status'] == 'completed' and runs.loc[ok, 'notes'] == 'baseline'
    assert runs.loc[ok, 'n_sims'] == 10 and runs.loc[ok, 'n_events'] == 1
    assert runs.loc[failed, 'status'] == 'failed' and runs.loc[failed, 'error'] == 'boom'
    assert br.run_settings(ok, store) == {'n_sims': 10}


def test_scoring():
    scored = br.score_predictions(_predictions().assign(run_id=1), n_values=(1, 3))
    a = scored[(scored['username'] == 'a') & (scored['n'] == 1)].iloc[0]
    b = scored[(scored['username'] == 'b') & (scored['n'] == 1)].iloc[0]
    assert a['hit'] == 1 and a['p_top'] == pytest.approx(0.5)
    assert b['hit'] == 0 and b['p_top'] == pytest.approx(0.25)

    summary = br.summarize(scored).loc[(1, 'score', 1)]
    assert summary['brier'] == pytest.approx((0.5 ** 2 + 0.25 ** 2) / 2)
    assert summary['predicted_per_event'] == pytest.approx(0.75)
    assert summary['actual_per_event'] == 1 and summary['hit_capture'] == 1

    attendance = br.attendance_summary(scored).loc[(1, 'score')]
    assert attendance['predicted_players_per_event'] == pytest.approx(1.5)
    assert attendance['actual_players_per_event'] == 1


def test_calibration_bins_compare_predicted_and_observed():
    scored = br.score_predictions(_predictions().assign(run_id=1), n_values=(1,))
    table = br.calibration(scored)
    assert table['count'].sum() == 2
    assert table.loc[table['predicted'] == 0.5, 'observed'].iloc[0] == 1


# ── Snapshot (needs a disposable Postgres) ───────────────────────────────────

@pytest.fixture
def postgres():
    url = os.environ.get('TEST_PG_URL')
    if not url:
        pytest.skip('TEST_PG_URL not set')
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute('DROP TABLE IF EXISTS snap_demo, trade_jobs CASCADE')
        conn.execute('CREATE TABLE snap_demo (id INTEGER PRIMARY KEY, "MixedCase" REAL, '
                     'ts TIMESTAMPTZ, info JSONB)')
        conn.execute("""INSERT INTO snap_demo VALUES
            (1, 0.5, '2026-11-03 00:00:00+00', '{"a": 1}'), (2, NULL, NULL, NULL)""")
        conn.execute('CREATE TABLE trade_jobs (id INTEGER)')
    yield url
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute('DROP TABLE IF EXISTS snap_demo, trade_jobs CASCADE')


def test_snapshot_copies_data_and_skips_operational_tables(postgres, tmp_path, monkeypatch):
    import pull_snapshot
    monkeypatch.delenv('DATABASE_URL', raising=False)
    dest = tmp_path / 'data' / 'copy.db'
    counts = pull_snapshot.pull(postgres, dest)
    assert counts['snap_demo'] == 2 and 'trade_jobs' not in counts

    conn = sqlite3.connect(dest)
    assert conn.execute('SELECT "MixedCase", ts, info FROM snap_demo WHERE id = 1').fetchone() == (
        0.5, '2026-11-03 00:00:00+00:00', '{"a": 1}')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO snap_demo (id) VALUES (1)')
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'bullet_brawl_standings'").fetchone()
    conn.close()


def test_snapshot_keeps_locally_backfilled_tables(postgres, tmp_path, monkeypatch):
    import pull_snapshot
    monkeypatch.delenv('DATABASE_URL', raising=False)
    dest = tmp_path / 'copy.db'
    pull_snapshot.pull(postgres, dest)
    conn = sqlite3.connect(dest)
    conn.execute("INSERT INTO bullet_brawl_standings (tournament_slug, username) VALUES ('s', 'u')")
    conn.commit()
    conn.close()

    pull_snapshot.pull(postgres, dest)
    conn = sqlite3.connect(dest)
    assert conn.execute('SELECT COUNT(*) FROM bullet_brawl_standings').fetchone()[0] == 1
    conn.close()


def test_snapshot_connection_is_read_only(postgres):
    import pull_snapshot
    import sqlalchemy
    from sqlalchemy import text
    engine = pull_snapshot._source_engine(postgres)
    with pytest.raises(sqlalchemy.exc.InternalError, match='read-only'):
        with engine.connect() as conn:
            conn.execute(text('DELETE FROM snap_demo'))
    engine.dispose()


def test_failed_snapshot_keeps_the_existing_file(postgres, tmp_path, monkeypatch):
    import pull_snapshot
    monkeypatch.delenv('DATABASE_URL', raising=False)
    dest = tmp_path / 'copy.db'
    dest.write_text('previous')

    def explode(*_args):
        raise RuntimeError('network dropped')

    monkeypatch.setattr(pull_snapshot, 'apply_local_schema', explode)
    with pytest.raises(RuntimeError):
        pull_snapshot.pull(postgres, dest)
    assert dest.read_text() == 'previous' and not dest.with_suffix('.partial').exists()


def _backtest_module():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
    import run_backtest
    return run_backtest


def test_bullet_brawl_backtest_sigmas_are_in_score_points():
    profile = _backtest_module().PROFILES['bullet_brawl']
    assert profile.noise_sigma == {'score': 0.0, 'score_smooth_3': 30_000.0, 'score_smooth_10': 100_000.0}


def test_actuals_leave_out_registered_players_who_never_played():
    import sqlite3
    run_backtest = _backtest_module()
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE bullet_brawl_standings (date TEXT, username TEXT, rank REAL, games INTEGER)')
    conn.executemany('INSERT INTO bullet_brawl_standings VALUES (?, ?, ?, ?)', [
        ('2026-11-07 00:00:00+00:00', 'played', 1.0, 20),
        ('2026-11-07 00:00:00+00:00', 'no_show', 90.0, 0),
    ])
    actuals = run_backtest._fetch_actuals(conn, run_backtest.BULLET_BRAWL, '2026-11-07')
    assert list(actuals.index) == ['played']
