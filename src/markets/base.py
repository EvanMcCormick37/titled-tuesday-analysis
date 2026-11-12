"""What differs between weekly top-N tournament markets.

Stdlib only, so any layer can import a spec without creating an import cycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ModelConfig:
    standings_table: str
    data_cutoff: str
    skill_decay: float
    participation_decay: float
    min_participation_rate: float
    season_shift_date: str | None = None
    season_shift_factor: float = 1.0
    score_noise_sigma: float = 0.0     # Gaussian noise on each sampled score, in score points
    played_filter: str | None = None   # SQL condition for a standings row that counts as attending


@dataclass(frozen=True)
class MarketSpec:
    slug: str
    label: str
    winner_series: str
    top_series: str
    top_ns: tuple[int, ...]
    weekday: int                 # Monday=0
    start_hour: int
    start_minute: int
    bid_expiry_hour: int
    bid_expiry_minute: int
    model: ModelConfig
    prediction_ns: tuple[int, ...] = (1, 3, 5, 8, 10)
    timezone: str = 'America/New_York'
    # Weekly pipeline steps, in order. The runner in scripts/weekly_pipeline.py and
    # the dashboard's Pipeline page both read this.
    pipeline_steps: tuple[str, ...] = ('scrape', 'update_broadcasts', 'run_raw')
    event_url_example: str = 'https://www.chess.com/tournament/live/…'

    @property
    def traded_ns(self) -> tuple[int, ...]:
        return (1, *self.top_ns)

    @property
    def event_templates(self) -> Mapping[str, int]:
        """Kalshi event ticker template → N. Winner first, then ascending top-N."""
        return {f'{self.winner_series}-{{date}}': 1,
                **{f'{self.top_series}-{{date}}T{n}': n for n in self.top_ns}}

    def ticker_pattern(self, kalshi_date: str) -> re.Pattern:
        """Matches any contract on this event date, including top-N levels not in `top_ns`.

        Deliberately broad: the kill switch must not miss a resting order because
        Kalshi listed a new top-N level.
        """
        return re.compile(rf'(?:{self.winner_series}-{kalshi_date}|{self.top_series}-{kalshi_date}T\d+)-')

    def n_from_ticker(self, ticker: str, kalshi_date: str) -> int | None:
        if f'{self.winner_series}-{kalshi_date}-' in ticker:
            return 1
        m = re.search(rf'{self.top_series}-{kalshi_date}T(\d+)-', ticker)
        return int(m.group(1)) if m else None

    def _local_ts(self, event_date: str, hour: int, minute: int) -> datetime:
        naive = datetime.strptime(event_date, '%Y-%m-%d')
        return naive.replace(hour=hour, minute=minute, tzinfo=ZoneInfo(self.timezone))

    def bid_expiry_ts(self, event_date: str) -> int:
        return int(self._local_ts(event_date, self.bid_expiry_hour, self.bid_expiry_minute).timestamp())

    def start_ts(self, event_date: str, minutes_after: int = 0) -> int:
        start = self._local_ts(event_date, self.start_hour, self.start_minute)
        return int((start + timedelta(minutes=minutes_after)).timestamp())

    def next_event_on_or_after(self, from_date: date | None = None) -> date:
        d = from_date or date.today()
        return d + timedelta(days=(self.weekday - d.weekday()) % 7)

    def most_recent_event(self, from_date: date | None = None) -> date:
        d = from_date or date.today()
        return d - timedelta(days=(d.weekday() - self.weekday) % 7)
