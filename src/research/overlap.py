"""Stage 2b: the clash test, and matching clashing events to candidates.

The event window is [start, start + spec.event_duration_minutes]. Each round of
another tournament is assumed to last a time-control-dependent length (classical
5 h by default). Then:

    hard        a round overlaps the window
    adjacent    a round ends or starts within `adjacent_hours` of the window
    multi_day   the player is mid-tournament: a round within `mid_event_hours` of the
                window (rest day, travel), or, with round times unknown, a tournament
                of at most `MAX_UNTIMED_SPAN_DAYS` that spans the event day. A season-long
                league spans every Tuesday, so it only counts through its actual rounds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..markets import MarketSpec
from .config import ResearchParams
from .names import better, hint_match, match_grade

KINDS = ('hard', 'adjacent', 'multi_day')
MAX_UNTIMED_SPAN_DAYS = 21
UNKNOWN_FIELD_MIN_TIER = 5


@dataclass(frozen=True)
class Window:
    event_date: str
    start: datetime
    end: datetime


def event_window(spec: MarketSpec, event_date: str) -> Window:
    start = datetime.fromtimestamp(spec.start_ts(event_date), timezone.utc)
    return Window(event_date, start, start + timedelta(minutes=spec.event_duration_minutes))


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify(event: dict, window: Window, params: ResearchParams) -> dict | None:
    """The event's clash with the window, or None. Returns the kind and the rounds near it."""
    length = timedelta(minutes=params.round_minutes(event.get('fide_tc')))
    reach = timedelta(hours=params.adjacent_hours)
    mid_event = timedelta(hours=params.mid_event_hours)
    timed = [r for r in event.get('rounds') or [] if r.get('starts_at')]
    kind, near = None, []
    for r in timed:
        s = _parse(r['starts_at'])
        e = s + length
        if s < window.end and e > window.start:
            overlap = (min(e, window.end) - max(s, window.start)).total_seconds() / 60
            near.append({**r, 'clash': 'hard', 'overlap_minutes': round(overlap)})
            kind = 'hard'
            continue
        gap = window.start - e if e <= window.start else s - window.end
        minutes = round(gap.total_seconds() / 60)
        if gap <= reach:
            near.append({**r, 'clash': 'adjacent', 'gap_minutes': minutes})
            kind = kind or 'adjacent'
        elif gap <= mid_event:
            near.append({**r, 'clash': 'mid_event', 'gap_minutes': minutes})
    if kind is None and any(r['clash'] == 'mid_event' for r in near):
        kind = 'multi_day'
    if kind is None and not timed:
        first, last = event.get('start_date'), event.get('end_date') or event.get('start_date')
        if first and first <= window.event_date <= last and \
                (date.fromisoformat(last) - date.fromisoformat(first)).days <= MAX_UNTIMED_SPAN_DAYS:
            kind = 'multi_day'
    return {'kind': kind, 'rounds': near} if kind else None


def _match(candidate: dict, event: dict) -> tuple[str | None, str | None]:
    grade, matched_as = None, None
    for p in event.get('players') or []:
        g = match_grade(candidate['player_name'], p['name'])
        if g and better(g, grade) == g:
            grade, matched_as = g, p['name']
    if grade is None and hint_match(candidate['player_name'], event.get('players_hint')):
        grade, matched_as = 'hint', event.get('players_hint')
    return grade, matched_as


def find_leads(candidates: list[dict], events: list[dict], window: Window, params: ResearchParams) -> list[dict]:
    """One lead per (candidate, clashing event), strongest clash and highest fair value first.

    A top-tier event (tier 5, or added by the operator) with a hard clash and no
    player information at all (no list, no players line) also yields a lead with
    no player, so it isn't missed before its pairings appear.
    """
    leads = []
    for ev in events:
        clash = classify(ev, window, params)
        if clash is None:
            continue
        base = {'event_id': ev.get('id'), 'event_name': ev['name'], 'event_url': ev.get('url'),
                'source': ev.get('source'), 'tier': ev.get('tier'), 'fide_tc': ev.get('fide_tc'),
                'time_zone': ev.get('time_zone'), 'policy': ev.get('policy', 'auto'),
                'policy_value': ev.get('policy_value'), 'kind': clash['kind'], 'rounds': clash['rounds']}
        matched = False
        for c in candidates:
            grade, matched_as = _match(c, ev)
            if grade:
                matched = True
                leads.append({**base, 'player_name': c['player_name'], 'username': c['username'],
                              'fair_value': c['fair_value'], 'p_participate': c['p_participate'],
                              'match': grade, 'matched_as': matched_as})
        elite = (ev.get('tier') or 0) >= UNKNOWN_FIELD_MIN_TIER or ev.get('source') == 'operator'
        unknown_field = not ev.get('players') and not ev.get('players_hint')
        if not matched and unknown_field and clash['kind'] == 'hard' and elite:
            leads.append({**base, 'player_name': None, 'username': None, 'fair_value': None,
                          'p_participate': None, 'match': None, 'matched_as': None})
    leads.sort(key=lambda l: (KINDS.index(l['kind']), l['player_name'] is None, -(l['fair_value'] or 0)))
    return leads
