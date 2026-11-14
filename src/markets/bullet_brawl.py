"""Bullet Brawl: 2-hour 1+0 arena, Saturdays at 12:00 ET.

The score model uses Gaussian smoothing of 10 score points, the best performer in
the walk-forward sigma comparison (`run_backtest.py --market bullet_brawl`). The
bid expiry mirrors Titled Tuesday's: two hours before the start.
"""
from __future__ import annotations

from .. import config
from .base import MarketSpec, ModelConfig

BULLET_BRAWL = MarketSpec(
    slug='bullet_brawl',
    label='Bullet Brawl',
    winner_series='KXBULLETBRAWL',
    top_series='KXBULLETBRAWLTOP',
    top_ns=(3, 5, 8),
    weekday=5,
    start_hour=12,
    start_minute=0,
    bid_expiry_hour=10,
    bid_expiry_minute=0,
    pipeline_steps=('scrape_bb', 'update_broadcasts', 'run_raw'),
    event_url_example='https://www.chess.com/tournament/live/arena/bullet-brawl-…',
    event_slug_pattern=r'bullet-brawl-[a-z]+-\d{2}-\d{4}-\d{5,}',
    model=ModelConfig(
        standings_table='bullet_brawl_standings',
        data_cutoff=config.DATA_CUTOFF,
        skill_decay=config.SKILL_DECAY,
        participation_decay=config.PARTICIPATION_DECAY,
        min_participation_rate=config.MIN_PARTICIPATION_RATE,
        score_noise_sigma=10.0,
        played_filter='games > 0',     # registered but never paired is not attending
    ),
)
