"""Fair values from the model store, validated and swapped in as a unit.

Source: `latest_model_predictions` for the market, the same table place_bids
priced from. fair(YES) = p_participate × P_top{N}_given_play.

A set is only used when the prior event's `pipeline_runs` row is `completed`
(the same stale-data guard as the trade queue) and no recompute is running,
since a recompute briefly empties the predictions table. The version string
changes whenever the predictions change, which is what triggers a top-down
reprice and clears fallback factors.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pandas as pd

from ..db import get_conn, read_market_rows
from ..markets import MarketSpec

N_TO_COL = {1: 'P_top1_given_play', 3: 'P_top3_given_play', 5: 'P_top5_given_play',
            8: 'P_top8_given_play', 10: 'P_top10_given_play'}
SUM_TOLERANCE = 0.15       # top-N probabilities must sum to N within ±15%


@dataclass
class FairValueSet:
    version: str
    tourn_date: str
    preds: pd.DataFrame                       # indexed by username
    fair: dict[str, dict[int, float]] = field(default_factory=dict)   # username → N → fair YES

    def fair_yes(self, username: str, n: int) -> float | None:
        return self.fair.get(username, {}).get(n)


def validate(preds: pd.DataFrame, ns: tuple[int, ...]) -> list[str]:
    """Data-bug checks, not second-guessing the model. Empty list = valid."""
    errors: list[str] = []
    if preds.empty:
        return ['no predictions']
    cols = [N_TO_COL[n] for n in sorted(ns) if N_TO_COL[n] in preds.columns]
    missing = [N_TO_COL[n] for n in ns if N_TO_COL[n] not in preds.columns]
    if missing or 'p_participate' not in preds.columns:
        return [f'missing columns: {missing + (["p_participate"] if "p_participate" not in preds.columns else [])}']
    for c in cols + ['p_participate']:
        bad = preds[(preds[c] < 0) | (preds[c] > 1) | preds[c].isna()]
        if len(bad):
            errors.append(f'{c} outside [0, 1] for {list(bad.index[:5])}')
    for lo, hi in zip(cols, cols[1:]):
        bad = preds[preds[lo] > preds[hi] + 1e-9]
        if len(bad):
            errors.append(f'{lo} > {hi} for {list(bad.index[:5])}')
    for n in ns:
        total = float((preds['p_participate'] * preds[N_TO_COL[n]]).sum())
        if abs(total - n) > SUM_TOLERANCE * n:
            errors.append(f'P(top {n}) sums to {total:.2f}, expected about {n}')
    return errors


def _source_version(spec: MarketSpec, prior_date: str) -> tuple[str | None, str | None]:
    """(state, completed_at) of the prior event's pipeline run."""
    with get_conn() as conn:
        row = conn.execute('SELECT state, completed_at FROM pipeline_runs WHERE market = ? '
                           'AND tourn_date = ?', (spec.slug, prior_date)).fetchone()
    return (row[0], str(row[1])) if row else (None, None)


def recompute_running(market: str) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM trade_jobs WHERE market = ? AND strategy = 'recompute' "
                            "AND status = 'running'", (market,)).fetchone() is not None


def load(spec: MarketSpec, tourn_date: str, prior_date: str) -> tuple[FairValueSet | None, list[str]]:
    """Returns (set, errors). (None, [reason]) means keep whatever is already in use."""
    state, completed_at = _source_version(spec, prior_date)
    if state != 'completed':
        return None, [f'pipeline for {prior_date} is {state or "missing"}, not completed']
    if recompute_running(spec.slug):
        return None, ['recompute running']
    preds = read_market_rows('latest_model_predictions', spec.slug)
    if 'username' in preds.columns:
        preds = preds.set_index('username')
    ns = spec.traded_ns
    errors = validate(preds, ns)
    if errors:
        return None, errors
    digest = hashlib.sha1(pd.util.hash_pandas_object(
        preds[['p_participate'] + [N_TO_COL[n] for n in ns]].round(6), index=True).values.tobytes()
    ).hexdigest()[:12]
    fv = FairValueSet(version=f'{completed_at}|{digest}', tourn_date=tourn_date, preds=preds)
    for username, row in preds.iterrows():
        p = float(row['p_participate'])
        fv.fair[str(username)] = {n: p * float(row[N_TO_COL[n]]) for n in ns}
    return fv, []
