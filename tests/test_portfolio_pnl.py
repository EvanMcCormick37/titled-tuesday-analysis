"""Portfolio P&L simulation behind the Portfolio page's `pnl` snapshot."""
from __future__ import annotations

import json
import subprocess

import numpy as np
import pandas as pd

from src import job_queue as q, market_views
from src.markets import TITLED_TUESDAY
from src.portfolio import build_portfolio, run_portfolio_mc_score, summarize_pnl
from tests.test_trade_jobs import DATE, MARKET, backend  # noqa: F401


def test_summarize_pnl_stats_and_histogram():
    pnl = np.array([-10.0] * 30 + [20.0] * 70)
    summary, hist = summarize_pnl(pnl, total_cost=10.0, bins=6)
    assert summary['ev'] == 11.0 and summary['expected_payout'] == 21.0
    assert summary['p_profit'] == 0.7 and summary['roi'] == 1.1
    assert summary['min'] == -10.0 and summary['max'] == 20.0
    assert sum(b['count'] for b in hist) == 100 and hist[0]['count'] == 30 and hist[-1]['count'] == 70


def test_summarize_pnl_single_outcome():
    summary, hist = summarize_pnl(np.full(50, -5.0), total_cost=5.0)
    assert hist == [{'lo': -5.0, 'hi': -5.0, 'count': 50}] and summary['p_profit'] == 0.0


def test_portfolio_mc_resolves_yes_no_and_unmatched():
    # 'a' always plays and always scores highest; 'b' never plays.
    players = ['a', 'b', 'c', 'd']
    p_play = np.array([1.0, 0.0, 1.0, 1.0])
    comps = [np.array([900.0]), np.array([800.0]), np.array([100.0]), np.array([50.0])]
    wts = [np.array([1.0])] * 4
    df = pd.DataFrame({
        'marketTitle': ['A', 'B', 'A', 'Ghost'],
        'n': [1, 3, 1, 3], 'position': ['Yes', 'Yes', 'No', 'No'],
        'volume': [10.0, 10.0, 10.0, 10.0], 'cost': [5.0, 1.0, 2.0, 1.0],
    })
    port = build_portfolio(df, players, {'A': 'a', 'B': 'b'})
    pnl, p_win = run_portfolio_mc_score(players, p_play, comps, wts, port,
                                        n_sims=500, chunk=200, noise_sigma=1.0, log=None)
    assert list(p_win) == [1.0, 0.0, 0.0, 0.0]
    assert np.allclose(pnl, 10.0 - 9.0)


def test_pnl_snapshot_reuses_result_until_inputs_change(backend, monkeypatch):  # noqa: F811
    positions = pd.DataFrame([{
        'ticker': 'T1', 'marketTitle': 'A', 'n': 8, 'position': 'Yes', 'volume': 10.0,
        'cost': 4.0, 'fees': 0.1, 'cost_incl_fees': 4.1, 'avg_price': 0.4, 'avg_price_incl_fees': 0.41,
    }])
    monkeypatch.setattr(market_views, 'get_positions_df', lambda *a, **k: positions)
    monkeypatch.setattr(market_views, '_fees_by_ticker', lambda *a, **k: {})
    preds = pd.DataFrame({'username': ['a'], 'tourn_date': [DATE], 'p_participate': [0.5]})
    monkeypatch.setattr(market_views, 'read_market_rows', lambda table, market: preds)
    calls = []

    def fake_run(cmd, input, **kwargs):
        calls.append(json.loads(input))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({'summary': {'ev': 1.0}}), stderr='')
    monkeypatch.setattr(market_views.subprocess, 'run', fake_run)

    class Client:
        def get_positions(self):
            return pd.DataFrame({'ticker': ['T1']})

    first = market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY)
    assert len(calls) == 1 and first['summary']['ev'] == 1.0 and first['input_key']
    q.save_snapshot(MARKET, 'pnl', first)
    assert market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY) == first
    assert len(calls) == 1

    positions.loc[0, 'volume'] = 20.0
    market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY)
    assert len(calls) == 2 and calls[1][0]['volume'] == 20.0

    q.save_snapshot(MARKET, 'pnl', market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY))
    assert len(calls) == 3        # unchanged inputs since the last save: still 3 after the next call
    market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY)
    assert len(calls) == 3
    preds.loc[0, 'p_participate'] = 0.9          # a recompute
    market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY)
    assert len(calls) == 4


def test_pnl_snapshot_without_positions(backend, monkeypatch):  # noqa: F811
    monkeypatch.setattr(market_views, 'get_positions_df', lambda *a, **k: pd.DataFrame())

    class Client:
        def get_positions(self):
            return pd.DataFrame()

    assert market_views.pnl_snapshot(Client(), DATE, TITLED_TUESDAY) == {'tourn_date': DATE, 'empty': True}
