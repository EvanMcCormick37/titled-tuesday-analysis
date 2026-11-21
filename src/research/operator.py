"""Events added by hand on the dashboard, e.g. from chess.com/events, which can't be read automatically."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OperatorEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=2, max_length=200)
    url: str | None = Field(None, max_length=500)
    start_date: date
    end_date: date | None = None
    round_time: str | None = Field(None, pattern=r'^\d{1,2}:\d{2}$',
                                   description='Daily round start, HH:MM in `time_zone`; omit if unknown')
    time_zone: str = 'UTC'
    fide_tc: str | None = Field(None, pattern=r'^(standard|rapid|blitz)$')
    players: list[str] = Field(default_factory=list, max_length=500)
    note: str | None = Field(None, max_length=1000)

    @field_validator('time_zone')
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f'unknown time zone {v!r} (use an IANA name such as Europe/Madrid)') from None
        return v

    @model_validator(mode='after')
    def _dates(self) -> 'OperatorEvent':
        if self.end_date and self.end_date < self.start_date:
            raise ValueError('end date is before start date')
        if self.end_date and (self.end_date - self.start_date).days > 60:
            raise ValueError('events longer than 60 days are not supported')
        return self

    def to_record(self) -> dict:
        end = self.end_date or self.start_date
        rounds = []
        if self.round_time:
            hh, mm = map(int, self.round_time.split(':'))
            tz = ZoneInfo(self.time_zone)
            d, n = self.start_date, 1
            while d <= end:
                local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
                rounds.append({'name': f'Day {n}', 'starts_at': local.astimezone(ZoneInfo('UTC')).isoformat(),
                               'finished': False})
                d, n = d + timedelta(days=1), n + 1
        return {
            'name': self.name, 'url': self.url, 'tier': None, 'time_zone': self.time_zone,
            'fide_tc': self.fide_tc, 'location': None,
            'start_date': self.start_date.isoformat(), 'end_date': end.isoformat(),
            'rounds': rounds, 'players': [{'name': p.strip()} for p in self.players if p.strip()],
            'players_hint': self.note,
        }
