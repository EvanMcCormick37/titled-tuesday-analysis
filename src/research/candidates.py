"""Stage 1: the players whose attendance moves money.

A candidate is a player whose raw-model fair value for the top-N contract,
p_participate × P(top N | plays), is above `candidate_min_fv` (default: top 8
above 5%). Raw, not adjusted, so a cut never hides a player from later runs.
Multiplying by p_participate drops strong players who rarely play: a clash
barely moves the price of someone who wasn't likely to show up anyway.
"""
from __future__ import annotations

import pandas as pd

from ..db import get_conn, read_market_rows
from ..markets import MarketSpec
from .config import ResearchParams


def load_candidates(spec: MarketSpec, params: ResearchParams, raw: pd.DataFrame | None = None) -> list[dict]:
    if params.candidate_n not in spec.prediction_ns:
        raise ValueError(f'candidate_n={params.candidate_n} is not one of {spec.label} '
                         f'prediction levels {spec.prediction_ns}')
    col = f'P_top{params.candidate_n}_given_play'
    raw = read_market_rows('latest_model_predictions_raw', spec.slug) if raw is None else raw
    if raw.empty:
        return []

    with get_conn() as conn:
        names = dict(conn.execute('SELECT username, player_name FROM player_information '
                                  'WHERE player_name IS NOT NULL').fetchall())

    df = raw.assign(fair_value=raw['p_participate'] * raw[col])
    df = df[df['fair_value'] > params.candidate_min_fv].copy()
    df['player_name'] = df['username'].map(names).fillna(df['username'])

    # One row per player: a player with several accounts keeps the most valuable one.
    df = df.sort_values('fair_value', ascending=False).drop_duplicates('player_name')
    keep = ['username', 'player_name', 'p_participate', 'fair_value',
            *[c for c in df.columns if c.startswith('P_top') and c.endswith('_given_play')]]
    def clean(v):
        if isinstance(v, float):
            return None if pd.isna(v) else round(float(v), 4)   # NaN isn't valid JSON for Postgres
        return v
    return [{k: clean(v) for k, v in row.items()} for row in df[keep].to_dict('records')]
