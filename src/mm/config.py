"""Market-maker parameters.

Defaults live here. A market's overrides are stored as JSON in
`mm_control.params` and re-read every few seconds, so they can be tuned from
the dashboard without restarting tt-mm. `MMParams` validates both.

Prices are integer cents throughout the market maker; sizes are whole contracts.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MMParams(BaseModel):
    model_config = ConfigDict(extra='forbid')

    # ── Anchor (deep) layer: today's place_bids pricing ──────────────────────
    anchor_volume: int = Field(200, ge=1, le=10_000, title='Anchor volume (AV)',
                               description='Contracts per side; scout + anchor never exceed this')
    markup: float = Field(1.5, ge=1.0, le=10.0, title='Markup', description='Anchor = fair / markup')
    max_discount: float = Field(15, ge=0, le=50, title='Max discount (¢)',
                                description='Anchor is never more than this many cents below fair')

    # ── Scout (top-of-book) layer ─────────────────────────────────────────────
    scouts_enabled: bool = Field(True, title='Scouts enabled')
    buffer_c: int = Field(2, ge=1, le=20, title='Buffer (¢)',
                          description='Scout never bids above fair − buffer. The attendance researcher '
                                      'also halts trading when a finding moves fair value by at least this much')
    min_scout: int = Field(1, ge=1, le=1_000, title='Min scout size')
    min_dwell_ms: int = Field(200, ge=0, le=5_000, title='Min dwell (ms)',
                              description='A move must persist this long before repricing, unless outbid')
    war_max_reprices: int = Field(8, ge=1, le=100, title='Price-war reprices/min',
                                  description='Above this, rest at the buffer edge for the cool-off')
    war_cooloff_s: int = Field(120, ge=10, le=3_600, title='Price-war cool-off (s)')

    # ── Fallback on anchor wipes (per player, as a ratio) ─────────────────────
    wipe_ratio: float = Field(0.33, gt=0, lt=1, title='Wipe cut',
                              description="Each wipe multiplies the player's fair value on that side by (1 − cut)")
    wipe_min_qty: int = Field(10, ge=1, title='Wipe min qty',
                              description='An anchor fill smaller than this never counts as a wipe')
    max_wipes: int = Field(2, ge=1, le=10, title='Wipes before pull',
                           description='This many wipes pull the player until reposted')
    wipe_mirror: bool = Field(True, title='Mirror on NO side',
                              description='A wiped NO anchor cuts the NO fair value the same way')

    # ── Risk ─────────────────────────────────────────────────────────────────
    max_position: int = Field(400, ge=1, title='Max position per market (contracts)')
    max_player_notional: float = Field(400, gt=0, title='Max resting $ per player per side')
    max_event_notional: float = Field(5_000, gt=0, title='Max resting $ per event')
    min_price_c: int = Field(2, ge=1, le=50, title='Lowest bid (¢)')
    max_price_c: int = Field(97, ge=50, le=99, title='Highest bid (¢)')
    order_group_limit: int = Field(2_000, ge=1, title='Order-group limit (contracts / 15 s)')

    # ── Timing ───────────────────────────────────────────────────────────────
    cutoff_minutes_before_start: int = Field(120, ge=0, le=24 * 60, title='Cutoff (min before start)')


DEFAULT_PARAMS = MMParams()

# Prefixes on client_order_id. Everything the market maker manages carries one,
# so its startup cancel and the watchdog never touch fade or manual orders.
ANCHOR_PREFIX = 'mma-'
SCOUT_PREFIX = 'mms-'
MM_PREFIXES = (ANCHOR_PREFIX, SCOUT_PREFIX)


def is_mm_order(order: dict, prefixes: tuple[str, ...] = MM_PREFIXES) -> bool:
    return str(order.get('client_order_id') or '').startswith(prefixes)
