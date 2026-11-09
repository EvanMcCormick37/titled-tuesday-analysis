"""Bullet Brawl: 2-hour 1+0 arena, Saturdays at 12:00 ET.

Not registered in `src.strategies.MARKETS` yet, so it cannot be traded. The model
values below are untuned starting points until the backtest sets them, and the
bid expiry simply mirrors Titled Tuesday's two hours before the start.
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
    model=ModelConfig(
        standings_table='bullet_brawl_standings',
        data_cutoff=config.DATA_CUTOFF,
        skill_decay=config.SKILL_DECAY,
        participation_decay=config.PARTICIPATION_DECAY,
        min_participation_rate=config.MIN_PARTICIPATION_RATE,
    ),
)
