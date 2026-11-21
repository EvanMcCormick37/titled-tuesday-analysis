"""Lichess broadcast collector: the researcher's automated tournament calendar.

Two public, documented endpoints list tournaments:
    GET /api/broadcast/top    every active and upcoming broadcast, all tiers, minor opens included
    GET /api/broadcast        official broadcasts as NDJSON, including ones scheduled months ahead
Every tier is kept: a top player can enter a minor open. Only events whose dates
come within `calendar_days` of the event day are inspected further:
    GET /api/broadcast/{id}          rounds with `startsAt` (skipped when the listing already has them)
    GET /broadcast/{id}/players      full names, titles, federations, FIDE IDs (empty before round 1)

Requests are sequential with a small gap, identify the client, and back off once
on HTTP 429, per Lichess's API guidelines.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests

from .config import ResearchParams

BASE_URL = 'https://lichess.org'
USER_AGENT = 'titled-tuesday-analysis attendance-researcher (python-requests)'
TIMEOUT_S = 20
RATE_LIMIT_WAIT_S = 60


class LichessClient:
    def __init__(self, gap_s: float = 0.6, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.gap_s = gap_s
        self.sleep = sleep
        self.requests = 0
        self._last = 0.0

    def _get(self, path: str, *, params: dict | None = None, accept: str = 'application/json',
             allow_404: bool = False) -> requests.Response | None:
        for attempt in range(2):
            wait = self.gap_s - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            resp = self.session.get(f'{BASE_URL}{path}', params=params, timeout=TIMEOUT_S,
                                    headers={'Accept': accept})
            self._last = time.monotonic()
            self.requests += 1
            if resp.status_code == 429 and attempt == 0:
                self.sleep(RATE_LIMIT_WAIT_S)
                continue
            if resp.status_code == 404 and allow_404:
                return None
            resp.raise_for_status()
            return resp
        return None

    def top(self) -> list[dict]:
        data = self._get('/api/broadcast/top', params={'page': 1}).json()
        return [*data.get('active', []), *data.get('upcoming', [])]

    def official(self, nb: int = 100) -> list[dict]:
        text = self._get('/api/broadcast', params={'nb': nb}, accept='application/x-ndjson').text
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def tour(self, tour_id: str) -> dict:
        return self._get(f'/api/broadcast/{tour_id}').json()

    def players(self, tour_id: str) -> list[dict]:
        resp = self._get(f'/broadcast/{tour_id}/players', allow_404=True)
        if resp is None:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


def _utc_date(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else None


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


def _date_range(tour: dict, rounds: list[dict]) -> tuple[str | None, str | None]:
    dates = [d for d in tour.get('dates') or [] if d]
    if not dates:
        dates = [r['startsAt'] for r in rounds if r.get('startsAt')]
    if not dates:
        return None, None
    return _utc_date(min(dates)), _utc_date(max(dates))


def _near(first: str | None, last: str | None, lo: str, hi: str) -> bool:
    return first is not None and first <= hi and (last or first) >= lo


def to_event(tour: dict, rounds: list[dict], players: list[dict]) -> dict:
    """Normalise a Lichess tour into a `research_events` record."""
    info = tour.get('info') or {}
    start, end = _date_range(tour, rounds)
    return {
        'source': 'lichess',
        'source_id': tour['id'],
        'name': tour.get('name') or tour['id'],
        'url': tour.get('url'),
        'tier': tour.get('tier'),
        'time_zone': info.get('timeZone'),
        'fide_tc': info.get('fideTC'),
        'location': info.get('location'),
        'start_date': start,
        'end_date': end,
        'rounds': [{'name': r.get('name'), 'starts_at': _iso(r.get('startsAt')),
                    'finished': bool(r.get('finished'))} for r in rounds],
        'players': [{'name': p.get('name'), 'title': p.get('title'), 'fed': p.get('fed'),
                     'fide_id': p.get('fideId'), 'rating': p.get('rating')}
                    for p in players if p.get('name')],
        'players_hint': info.get('players'),
    }


def collect(event_date: date, params: ResearchParams, client: LichessClient | None = None,
            log: Callable[[str], None] = print) -> tuple[list[dict], dict[str, Any]]:
    """Events near `event_date`, as `research_events` records, plus collector stats."""
    client = client or LichessClient(gap_s=params.lichess_request_gap_s)
    lo = (event_date - timedelta(days=params.calendar_days)).isoformat()
    hi = (event_date + timedelta(days=params.calendar_days)).isoformat()

    listed: dict[str, dict] = {}
    for entry in client.top():                       # {"tour": ..., "round": ...}
        listed.setdefault(entry['tour']['id'], {'tour': entry['tour'], 'rounds': None})
    for entry in client.official():                  # {"tour": ..., "rounds": [...]}
        tid = entry['tour']['id']
        listed[tid] = {'tour': entry['tour'], 'rounds': entry.get('rounds') or listed.get(tid, {}).get('rounds')}

    near = [v for v in listed.values() if _near(*_date_range(v['tour'], v['rounds'] or []), lo, hi)]
    near.sort(key=lambda v: -(v['tour'].get('tier') or 0))
    skipped = max(0, len(near) - params.lichess_max_tours)
    near = near[:params.lichess_max_tours]
    log(f'Lichess: {len(listed)} broadcasts listed, {len(near)} within {lo}..{hi}'
        + (f' ({skipped} lowest-tier skipped by lichess_max_tours)' if skipped else ''))

    events, failures = [], 0
    for v in near:
        tour, rounds = v['tour'], v['rounds']
        try:
            if rounds is None:
                detail = client.tour(tour['id'])
                tour, rounds = {**tour, **detail.get('tour', {})}, detail.get('rounds', [])
            players = client.players(tour['id'])
        except requests.RequestException as e:
            failures += 1
            log(f'  ! {tour.get("name")}: {e}')
            continue
        events.append(to_event(tour, rounds, players))

    stats = {'listed': len(listed), 'near': len(near), 'skipped_by_cap': skipped,
             'collected': len(events), 'failed': failures, 'requests': client.requests}
    return events, stats
