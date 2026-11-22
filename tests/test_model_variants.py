"""VIP scoring, the two-part attendance model, and the skill prior."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import backtest_runs as br
from src.attendance_model import two_part_participation
from src.markets import FREESTYLE_FRIDAY, TITLED_TUESDAY
from src.simulation import build_player_pool_score


# ── VIP scoring ──────────────────────────────────────────────────────────────

def _predictions(price_a: float) -> pd.DataFrame:
    rows = []
    for d in ('2026-01-08', '2026-01-15'):
        rows.append({'run_id': 1, 'model': 'm', 'tourn_date': d, 'username': 'A1', 'p_participate': 1.0,
                     **{f'P_top{n}_given_play': price_a for n in br.VIP_N_VALUES}})
    return pd.DataFrame(rows)


_STANDINGS = pd.DataFrame({
    'd':        ['2026-01-01', '2026-01-08', '2026-01-08', '2026-01-15', '2026-01-15'],
    'username': ['A1',         'A2',         'X',          'X',          'A1'],
    'rank':     [1,            1,            2,            1,            9],
})
_ACCOUNTS = {'A1': 'Player A', 'A2': 'Player A', 'B1': 'Player B'}


def test_vip_frame_sums_accounts_counts_missing_players_and_builds_the_baseline():
    f = br.vip_frame(_predictions(0.5), _STANDINGS, _ACCOUNTS)
    assert set(f['player']) == {'Player A', 'Player B'}
    b = f[f['player'] == 'Player B']
    assert (b['price1'] == 0).all() and (b['played'] == 0).all()          # no prediction → 0
    a = f[f['player'] == 'Player A'].set_index('tourn_date')
    assert a.loc['2026-01-08', 'hit1'] == 1                                # won on an alt account
    assert a.loc['2026-01-15', 'hit1'] == 0 and a.loc['2026-01-15', 'played'] == 1
    assert a.loc['2026-01-08', 'base1'] == 1.0                             # 1 win in 1 prior event
    assert a.loc['2026-01-15', 'base1'] == 1.0                             # 2 wins in 2 prior events


def test_vip_summary_prefers_the_sharper_forecast():
    f = pd.concat([br.vip_frame(_predictions(0.5), _STANDINGS, _ACCOUNTS),
                   br.vip_frame(_predictions(0.9), _STANDINGS, _ACCOUNTS).assign(model='sharp')])
    s = br.vip_summary(f)
    assert s.loc[(1, 'm', '1'), 'brier'] == pytest.approx(((0.5 - 1) ** 2 + 0.5 ** 2 + 0 + 0) / 4)
    assert set(s.index.get_level_values('n')) == {'1', '3', '5', '8', 'all'}


# ── Two-part attendance ──────────────────────────────────────────────────────

def _history(n_events=40):
    dates = pd.date_range('2025-01-03', periods=n_events, freq='7D')
    rng = np.random.default_rng(0)
    # A field with random attendance, half of which quits at some point, so the
    # model has rows to fit and recency carries information, as it does in real data.
    rates = rng.uniform(0.05, 0.95, 60)
    quits = np.where(np.arange(60) % 2 == 0, rng.integers(5, n_events, 60), n_events)
    rows = []
    for i, d in enumerate(dates):
        rows.append(('loyal', d))
        if i < n_events - 10:
            rows.append(('lapsed', d))            # stopped ten weeks ago
        if i % 2 == 0:
            rows.append(('casual', d))            # plays every other week
        rows += [(f'f{k}', d) for k in range(60) if i < quits[k] and rng.random() < rates[k]]
    return pd.DataFrame(rows, columns=['username', 'date']), dates[-1] + pd.Timedelta(days=7)


def test_two_part_ranks_loyal_over_casual_over_lapsed():
    df, ref = _history()
    p = two_part_participation(df, ref, conflicts=set())
    assert p['loyal'] > p['casual'] > p['lapsed']
    assert ((p >= 0.005) & (p <= 1)).all()


def test_two_part_ignores_conflict_weeks_as_targets_and_needs_history():
    df, ref = _history()
    # casual misses exactly the weeks they had a conflict: once those are set
    # aside, they look like a player who always comes when available.
    conflicts = {(d.strftime('%Y-%m-%d'), 'casual') for d in sorted(df['date'].unique())[1::2]}
    p = two_part_participation(df, ref, conflicts)
    assert p['casual'] > two_part_participation(df, ref, set())['casual']
    assert two_part_participation(df[df['date'] < df['date'].min() + pd.Timedelta(weeks=3)], ref, set()) is None


# ── Skill prior ──────────────────────────────────────────────────────────────

def _pool_df():
    dates = pd.date_range('2026-01-02', periods=10, freq='7D')
    rows = [{'username': 'star', 'date': d, 'score': 10.0, 'tie_break': 0.0, 'skill_w': 1.0, 'tournament_slug': str(d)}
            for d in dates]
    rows += [{'username': 'rookie', 'date': d, 'score': 10.0, 'tie_break': 0.0, 'skill_w': 1.0,
              'tournament_slug': str(d)} for d in dates[-5:]]
    rows += [{'username': f'f{k}', 'date': d, 'score': float(k % 8), 'tie_break': 0.0, 'skill_w': 1.0,
              'tournament_slug': str(d)} for k in range(20) for d in dates]
    df = pd.DataFrame(rows)
    p = pd.Series(0.5, index=df['username'].unique())
    apps = df.groupby('username').size()
    return df, p, apps


def test_skill_prior_pulls_short_histories_harder():
    df, p, apps = _pool_df()
    players, _, signals, weights = build_player_pool_score(df, p, apps, min_appearances=5, prior_events=2.0)
    field_weight = {u: w[len(df[df['username'] == u]):].sum() for u, w in zip(players, weights)}
    assert field_weight['star'] == pytest.approx(2 / 12)
    assert field_weight['rookie'] == pytest.approx(2 / 7)
    assert all(np.isclose(w.sum(), 1.0) for w in weights)
    plain = build_player_pool_score(df, p, apps, min_appearances=5)
    assert len(plain[2][players.index('star')]) == 10


def test_model_options_per_market():
    assert TITLED_TUESDAY.model.attendance_model == 'ewma'
    assert TITLED_TUESDAY.model.skill_prior_events == 0.0
    assert TITLED_TUESDAY.model.conflict_deduction is True
    assert FREESTYLE_FRIDAY.model.attendance_model == 'two_part'
    assert FREESTYLE_FRIDAY.model.skill_prior_events == 3.0
    assert FREESTYLE_FRIDAY.model.conflict_deduction is True
