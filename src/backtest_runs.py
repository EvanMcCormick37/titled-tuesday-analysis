"""Backtest run history and scoring. Local analysis only.

Every run gets its own row in `data/backtests.db` with the settings and git commit
that produced it, so runs can be compared instead of overwriting each other.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

N_VALUES = (1, 3, 5, 8, 10)
_EPS = 1e-6
_CALIBRATION_EDGES = (0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0)
_PROB_COLS = ', '.join(f'"P_top{n}_given_play" REAL' for n in N_VALUES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.BACKTEST_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            market      TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            git_commit  TEXT,
            git_dirty   INTEGER,
            models      TEXT NOT NULL,
            settings    TEXT NOT NULL,
            notes       TEXT,
            n_events    INTEGER NOT NULL DEFAULT 0,
            error       TEXT
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS backtest_predictions (
            run_id        INTEGER NOT NULL REFERENCES backtest_runs(run_id),
            tourn_date    TEXT NOT NULL,
            model         TEXT NOT NULL,
            username      TEXT NOT NULL,
            p_participate REAL,
            {_PROB_COLS},
            played        INTEGER,
            actual_rank   REAL,
            PRIMARY KEY (run_id, tourn_date, model, username)
        )
    """)
    return conn


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(['git', *args], cwd=config.PROJECT_ROOT, capture_output=True,
                                text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def start_run(conn: sqlite3.Connection, market: str, models: tuple[str, ...], settings: dict,
              notes: str | None = None) -> int:
    status = _git('status', '--porcelain')
    cur = conn.execute(
        'INSERT INTO backtest_runs (market, status, started_at, git_commit, git_dirty, models, settings, notes) '
        "VALUES (?, 'running', ?, ?, ?, ?, ?, ?)",
        (market, _now(), _git('rev-parse', '--short', 'HEAD'), None if status is None else int(bool(status)),
         json.dumps(list(models)), json.dumps(settings, sort_keys=True), notes),
    )
    conn.commit()
    return cur.lastrowid


def save_predictions(conn: sqlite3.Connection, run_id: int, predictions: pd.DataFrame) -> None:
    predictions.assign(run_id=run_id).to_sql('backtest_predictions', conn, if_exists='append', index=False)


def record_event(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute('UPDATE backtest_runs SET n_events = n_events + 1 WHERE run_id = ?', (run_id,))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: int, error: str | None = None) -> None:
    conn.execute('UPDATE backtest_runs SET status = ?, ended_at = ?, error = ? WHERE run_id = ?',
                 ('failed' if error else 'completed', _now(), error, run_id))
    conn.commit()


def list_runs(path: Path | None = None) -> pd.DataFrame:
    with connect(path) as conn:
        runs = pd.read_sql_query('SELECT * FROM backtest_runs ORDER BY run_id DESC', conn)
    settings = runs['settings'].map(json.loads)
    for key in ('n_sims', 'start_date', 'attendance_start'):
        runs[key] = settings.map(lambda s: s.get(key))
    return runs.drop(columns='settings').set_index('run_id')


def run_settings(run_id: int, path: Path | None = None) -> dict:
    with connect(path) as conn:
        row = conn.execute('SELECT settings FROM backtest_runs WHERE run_id = ?', (run_id,)).fetchone()
    if row is None:
        raise KeyError(f'No backtest run {run_id}')
    return json.loads(row[0])


def load_predictions(run_ids: list[int], path: Path | None = None) -> pd.DataFrame:
    marks = ', '.join('?' * len(run_ids))
    with connect(path) as conn:
        return pd.read_sql_query(f'SELECT * FROM backtest_predictions WHERE run_id IN ({marks})',
                                 conn, params=list(run_ids))


# ── Scoring ──────────────────────────────────────────────────────────────────

def _log_loss(y: pd.Series, p: pd.Series) -> float:
    p = p.clip(_EPS, 1 - _EPS)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def score_predictions(predictions: pd.DataFrame, n_values: tuple[int, ...] = N_VALUES) -> pd.DataFrame:
    """One row per (run, model, event, player, N), with the probability and outcome."""
    frames = []
    for n in n_values:
        given = predictions[f'P_top{n}_given_play']
        frames.append(pd.DataFrame({
            'run_id': predictions['run_id'],
            'model': predictions['model'],
            'tourn_date': predictions['tourn_date'],
            'username': predictions['username'],
            'n': n,
            'p_participate': predictions['p_participate'],
            'p_given_play': given,
            'p_top': predictions['p_participate'] * given,
            'played': predictions['played'],
            'hit': ((predictions['played'] == 1) & (predictions['actual_rank'] <= n)).astype(int),
        }))
    return pd.concat(frames, ignore_index=True)


def summarize(scored: pd.DataFrame) -> pd.DataFrame:
    """Accuracy per run, model and N. Lower log loss and Brier are better.

    `hit_capture` is the share of real top-N finishes that went to a player in the
    prediction pool. Below 1.0 means some finishers were never given a probability.
    """
    rows = []
    for (run_id, model, n), g in scored.groupby(['run_id', 'model', 'n']):
        events = g['tourn_date'].nunique()
        played = g[g['played'] == 1]
        rows.append({
            'run_id': run_id, 'model': model, 'n': n, 'events': events,
            'log_loss': _log_loss(g['hit'], g['p_top']),
            'brier': float(((g['p_top'] - g['hit']) ** 2).mean()),
            'log_loss_given_play': _log_loss(played['hit'], played['p_given_play']) if len(played) else np.nan,
            'predicted_per_event': g['p_top'].sum() / events,
            'actual_per_event': g['hit'].sum() / events,
            'hit_capture': g['hit'].sum() / (n * events),
        })
    return pd.DataFrame(rows).set_index(['run_id', 'model', 'n'])


def attendance_summary(scored: pd.DataFrame) -> pd.DataFrame:
    """How well p_participate predicted who showed up, across the prediction pool."""
    rows = []
    one_n = scored[scored['n'] == scored['n'].min()]
    for (run_id, model), g in one_n.groupby(['run_id', 'model']):
        events = g['tourn_date'].nunique()
        rows.append({
            'run_id': run_id, 'model': model, 'events': events,
            'log_loss': _log_loss(g['played'], g['p_participate']),
            'predicted_players_per_event': g['p_participate'].sum() / events,
            'actual_players_per_event': g['played'].sum() / events,
        })
    return pd.DataFrame(rows).set_index(['run_id', 'model'])


def calibration(scored: pd.DataFrame, probability: str = 'p_top', outcome: str = 'hit') -> pd.DataFrame:
    """Predicted vs observed rate in probability bins, per run, model and N."""
    binned = scored.assign(bin=pd.cut(scored[probability], _CALIBRATION_EDGES, include_lowest=True))
    return (binned.groupby(['run_id', 'model', 'n', 'bin'], observed=True)
            .agg(predicted=(probability, 'mean'), observed=(outcome, 'mean'), count=(outcome, 'size'))
            .reset_index())


# ── VIP scoring ──────────────────────────────────────────────────────────────
# Accuracy on the players actually traded (src.vip_players), not the whole field.
# The cohort is fixed before scoring, so no model can choose which players count.

VIP_N_VALUES = (1, 3, 5, 8)
VIP_BASELINE_WEEKS = 52
_VIP_PRICE_EDGES = (0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 1.0)


def vip_frame(predictions: pd.DataFrame, standings: pd.DataFrame, vip_accounts: dict[str, str],
              n_values: tuple[int, ...] = VIP_N_VALUES) -> pd.DataFrame:
    """One row per (run, model, event, VIP player): the traded price for each N and the outcome.

    `standings` has columns d (YYYY-MM-DD), username, rank, for every event of the
    market (earlier ones feed the baseline). A player's accounts are summed (only
    one plays in an event) and capped at 1; a VIP without a prediction counts as 0.
    `base{n}` is the baseline: the player's top-N rate over the market's events in
    the previous VIP_BASELINE_WEEKS weeks, counting an event they skipped as a miss.
    """
    players = sorted(set(vip_accounts.values()))
    st = standings.assign(player=standings['username'].map(vip_accounts)).dropna(subset=['player'])
    best = st.groupby(['d', 'player'])['rank'].min()
    all_dates = np.array(sorted(standings['d'].unique()))

    p = predictions.assign(player=predictions['username'].map(vip_accounts)).dropna(subset=['player'])
    for n in n_values:
        p[f'price{n}'] = p['p_participate'] * p[f'P_top{n}_given_play']
    cols = ['p_participate'] + [f'price{n}' for n in n_values]
    agg = p.groupby(['run_id', 'model', 'tourn_date', 'player'])[cols].sum().clip(upper=1.0)

    keys = predictions[['run_id', 'model', 'tourn_date']].drop_duplicates()
    grid = keys.merge(pd.DataFrame({'player': players}), how='cross')
    out = grid.merge(agg.reset_index(), on=['run_id', 'model', 'tourn_date', 'player'], how='left')
    out[cols] = out[cols].fillna(0.0)
    out['rank'] = [best.get((d, pl), np.nan) for d, pl in zip(out['tourn_date'], out['player'])]
    out['played'] = out['rank'].notna().astype(int)

    hits = best.reset_index()
    window = {d: all_dates[(all_dates < d) & (all_dates >= (pd.Timestamp(d) - pd.Timedelta(weeks=VIP_BASELINE_WEEKS))
                                              .strftime('%Y-%m-%d'))] for d in out['tourn_date'].unique()}
    for n in n_values:
        out[f'hit{n}'] = (out['rank'] <= n).astype(int)
        by_player = hits[hits['rank'] <= n].groupby('player')['d'].apply(lambda s: set(s)).to_dict()
        out[f'base{n}'] = [len(by_player.get(pl, set()).intersection(window[d])) / max(1, len(window[d]))
                           for d, pl in zip(out['tourn_date'], out['player'])]
    return out


def vip_summary(frame: pd.DataFrame, n_values: tuple[int, ...] = VIP_N_VALUES) -> pd.DataFrame:
    """VIP Brier per run, model and N (plus 'all' N pooled), against the frequency baseline.

    skill = 1 - brier / baseline_brier; above 0 means the model beats the baseline.
    """
    rows = []
    for (run_id, model), g in frame.groupby(['run_id', 'model']):
        events = g['tourn_date'].nunique()
        pooled = {'sq': 0.0, 'base_sq': 0.0, 'count': 0}
        for n in n_values:
            sq = ((g[f'price{n}'] - g[f'hit{n}']) ** 2)
            base_sq = ((g[f'base{n}'] - g[f'hit{n}']) ** 2)
            pooled['sq'] += sq.sum(); pooled['base_sq'] += base_sq.sum(); pooled['count'] += len(g)
            rows.append({'run_id': run_id, 'model': model, 'n': str(n), 'events': events,
                         'brier': sq.mean(), 'baseline_brier': base_sq.mean(),
                         'skill': 1 - sq.mean() / base_sq.mean(),
                         'predicted_per_event': g[f'price{n}'].sum() / events,
                         'actual_per_event': g[f'hit{n}'].sum() / events})
        rows.append({'run_id': run_id, 'model': model, 'n': 'all', 'events': events,
                     'brier': pooled['sq'] / pooled['count'], 'baseline_brier': pooled['base_sq'] / pooled['count'],
                     'skill': 1 - pooled['sq'] / pooled['base_sq'],
                     'predicted_per_event': np.nan, 'actual_per_event': np.nan})
    return pd.DataFrame(rows).set_index(['run_id', 'model', 'n'])


def vip_calibration(frame: pd.DataFrame, n_values: tuple[int, ...] = VIP_N_VALUES) -> pd.DataFrame:
    """Predicted vs observed VIP hit rate by traded price (all N pooled, prices >= 2%)."""
    long = pd.concat([frame[['run_id', 'model']].assign(price=frame[f'price{n}'], hit=frame[f'hit{n}'])
                      for n in n_values], ignore_index=True)
    long = long[long['price'] >= _VIP_PRICE_EDGES[0]]
    long['bin'] = pd.cut(long['price'], _VIP_PRICE_EDGES, include_lowest=True)
    return (long.groupby(['run_id', 'model', 'bin'], observed=True)
            .agg(predicted=('price', 'mean'), observed=('hit', 'mean'), count=('hit', 'size')).reset_index())


def vip_attendance(frame: pd.DataFrame) -> pd.DataFrame:
    """Predicted vs observed VIP attendance by p_participate."""
    binned = frame.assign(bin=pd.cut(frame['p_participate'], (0, .25, .5, .75, .9, 1.0), include_lowest=True))
    return (binned.groupby(['run_id', 'model', 'bin'], observed=True)
            .agg(predicted=('p_participate', 'mean'), observed=('played', 'mean'), count=('played', 'size'))
            .reset_index())


def vip_bootstrap(frame: pd.DataFrame, model_a: str, model_b: str, run_a: int, run_b: int | None = None,
                  n_values: tuple[int, ...] = VIP_N_VALUES, draws: int = 5000, seed: int = 0) -> np.ndarray:
    """2.5/50/97.5 percentiles of (Brier_a / Brier_b - 1), resampling events. Below 0: a is better.

    model_b may be 'baseline' to compare model_a with the frequency baseline.
    """
    def per_event(run, model, col):
        g = frame[(frame['run_id'] == run) & (frame['model'] == (model_a if col == 'base' else model))]
        sq = sum((g[f'{col}{n}'] - g[f'hit{n}']) ** 2 for n in n_values)
        return sq.groupby(g['tourn_date']).sum()
    a = per_event(run_a, model_a, 'price')
    b = per_event(run_a, model_a, 'base') if model_b == 'baseline' else per_event(run_b or run_a, model_b, 'price')
    dates = a.index.intersection(b.index).to_numpy()
    rng = np.random.default_rng(seed)
    ratios = []
    for _ in range(draws):
        s = rng.choice(dates, len(dates))
        ratios.append(a[s].sum() / b[s].sum() - 1)
    return np.percentile(ratios, [2.5, 50, 97.5])
