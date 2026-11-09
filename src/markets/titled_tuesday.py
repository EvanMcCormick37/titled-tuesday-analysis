"""Titled Tuesday: 11-round Swiss blitz, Tuesdays at 11:00 ET."""
from __future__ import annotations

from .. import config
from .base import MarketSpec, ModelConfig

TITLED_TUESDAY = MarketSpec(
    slug='titled_tuesday',
    label='Titled Tuesday',
    winner_series='KXTITLEDTUESDAY',
    top_series='KXTITLEDTUESTOP',
    top_ns=(3, 5, 8),
    weekday=1,
    start_hour=11,
    start_minute=0,
    bid_expiry_hour=9,
    bid_expiry_minute=0,
    model=ModelConfig(
        standings_table='titled_tuesday_standings',
        data_cutoff=config.DATA_CUTOFF,
        skill_decay=config.SKILL_DECAY,
        participation_decay=config.PARTICIPATION_DECAY,
        min_participation_rate=config.MIN_PARTICIPATION_RATE,
        season_shift_date=config.SEASON_SHIFT_DATE,
        season_shift_factor=config.SEASON_SHIFT_FACTOR,
    ),
)
