"""Freestyle Friday: 11-round Swiss, chess960 at 3+2, open to titled players, Fridays at 11:00 ET.

A regular chess.com live tournament (slug `freestyle-friday-{month}-{dd}-{yyyy}-{id}`),
so its standings have the same shape as Titled Tuesday's and the same score model
applies. Smoothing is Titled Tuesday's narrow kernel (100 composite units), which
randomizes tiebreaks without moving anyone across a score bucket. There is no season
shift: that adjustment is specific to Titled Tuesday's CCT season. The bid expiry
mirrors Titled Tuesday's, two hours before the start.

Attendance uses the two-part model (src/attendance_model.py) and skill histories get
a field prior of 3 events: with plain EWMA attendance and no prior, players rated
over 90% to attend showed up about 70% of the time and prices above 35% landed
about 25%.
"""
from __future__ import annotations

from .. import config
from .base import MarketSpec, ModelConfig

FREESTYLE_FRIDAY = MarketSpec(
    slug='freestyle_friday',
    label='Freestyle Friday',
    winner_series='KXFSFRIDAY',
    top_series='KXFSFRIDAYTOP',
    top_ns=(3, 5, 8),
    weekday=4,
    start_hour=11,
    start_minute=0,
    bid_expiry_hour=9,
    bid_expiry_minute=0,
    event_duration_minutes=160,     # 3+2 runs a little longer than Titled Tuesday's 3+1
    pipeline_steps=('scrape_ff', 'update_broadcasts', 'run_raw', 'research'),
    event_url_example='https://www.chess.com/tournament/live/freestyle-friday-…',
    event_slug_pattern=r'freestyle-friday-[a-z]+-\d{2}-\d{4}-\d{5,}',
    model=ModelConfig(
        standings_table='freestyle_friday_standings',
        data_cutoff=config.DATA_CUTOFF,
        skill_decay=config.SKILL_DECAY,
        participation_decay=config.PARTICIPATION_DECAY,
        min_participation_rate=config.MIN_PARTICIPATION_RATE,
        score_noise_sigma=config.SCORE_GAUSSIAN_KERNEL_SIGMA_NARROW / config.SCORE_COMPOSITE_SCALE,
        # The best of the VIP Brier comparison (backtest run 19, `narrow_two_part_prior3`):
        # 2% better than the plain EWMA model on the traded cohort, and the only variant
        # that clearly beat each player's own top-N frequency.
        attendance_model='two_part',
        skill_prior_events=3.0,
    ),
)
