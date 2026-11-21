"""Researcher parameters. Defaults here; per-market overrides in `research_control.params`."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ResearchParams(BaseModel):
    model_config = ConfigDict(extra='forbid')

    # ── Candidates ───────────────────────────────────────────────────────────
    candidate_min_fv: float = Field(0.05, gt=0, lt=1, title='Min top-N fair value',
                                    description='Research players whose raw-model fair value '
                                                '(p_participate × P(top N | plays)) is above this')
    candidate_n: int = Field(8, title='Top-N contract used for importance')

    # ── Clash test ───────────────────────────────────────────────────────────
    adjacent_hours: float = Field(4, ge=0, le=24, title='Adjacent window (h)',
                                  description='A round within this many hours of the event window is a soft clash')
    mid_event_hours: float = Field(36, ge=0, le=96, title='Mid-event window (h)',
                                   description='A round within this many hours means the player is mid-tournament '
                                               '(rest day or travel)')
    standard_round_minutes: int = Field(300, ge=30, le=480, title='Classical round length (min)')
    rapid_round_minutes: int = Field(75, ge=15, le=240, title='Rapid round length (min)')
    blitz_round_minutes: int = Field(30, ge=5, le=120, title='Blitz round length (min)')
    calendar_days: int = Field(1, ge=0, le=7, title='Calendar margin (days)',
                               description='Collect events whose dates fall within this many days of the event day')

    # ── Lichess collector ────────────────────────────────────────────────────
    lichess_request_gap_s: float = Field(0.6, ge=0, le=10, title='Gap between Lichess requests (s)')
    lichess_max_tours: int = Field(150, ge=1, le=500, title='Max tournaments to inspect per run',
                                   description='A safety cap; the run log says if it ever drops events')

    def round_minutes(self, fide_tc: str | None) -> int:
        return {'standard': self.standard_round_minutes, 'rapid': self.rapid_round_minutes,
                'blitz': self.blitz_round_minutes}.get(fide_tc or '', self.standard_round_minutes)


DEFAULT_PARAMS = ResearchParams()
