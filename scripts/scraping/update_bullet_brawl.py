#!/usr/bin/env python3
"""Load new Bullet Brawl results, then enrich new regulars via the chess.com PubAPI.

Chess.com has no listing page for Bullet Brawl, so new events are found in the
monthly game archives of the event's most frequent recent players: every arena
game there links its tournament. Each candidate is confirmed against the
tournament API (finished, 1+0, started on its slug's date) before it is stored.

Works on SQLite and Postgres. Each event replaces its own rows, so a rerun is safe.

Usage:
    python scripts/scraping/update_bullet_brawl.py                 # discover and load new events
    python scripts/scraping/update_bullet_brawl.py <url-or-slug>   # load one event
    python scripts/scraping/update_bullet_brawl.py --enrich-only   # just fill in player names
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts' / 'scraping'))

import requests

from src.bullet_brawl_ingest import (
    API, HEADERS, event_day, fetch_standings_pages, get_json, parse_round,
    slug_date, slug_from_url, slugs_in_archive, write_event,
)
from src.config import MIN_APPEARANCES
from src.db import get_conn
from src.markets import BULLET_BRAWL
from update_titled_tuesday import ListingMissingSlugError, enrich_players

ARCHIVE = 'https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}'
PAUSE_SECONDS = 1.0
REGULARS = 15           # players whose archives are searched
RECENT_EVENTS = 8       # how far back "regular" looks


def known_slugs() -> set[str]:
    with get_conn() as conn:
        return {r[0] for r in conn.execute('SELECT tournament_slug FROM bullet_brawl_tournaments').fetchall()}


def latest_event_date() -> date | None:
    with get_conn() as conn:
        last = conn.execute('SELECT MAX(date) FROM bullet_brawl_tournaments').fetchone()[0]
    return date.fromisoformat(str(last)[:10]) if last else None


def regulars(limit: int = REGULARS) -> list[str]:
    """Players who appeared in the most of the latest events, most frequent first."""
    with get_conn() as conn:
        dates = [r[0] for r in conn.execute(
            'SELECT date FROM bullet_brawl_tournaments ORDER BY date DESC LIMIT ?', (RECENT_EVENTS,)
        ).fetchall()]
        if not dates:
            return []
        marks = ', '.join('?' * len(dates))
        rows = conn.execute(
            f'SELECT username, COUNT(*) AS n, MIN(rank) AS best FROM bullet_brawl_standings '
            f'WHERE games > 0 AND date IN ({marks}) GROUP BY username ORDER BY n DESC, best ASC LIMIT ?',
            (*dates, limit),
        ).fetchall()
    return [r[0] for r in rows]


def missing_dates(today: date | None = None) -> list[date]:
    """Event days after the latest stored one, up to the most recent."""
    last = latest_event_date()
    newest = BULLET_BRAWL.most_recent_event(today)
    if last is None:
        return [newest]
    out, d = [], last + timedelta(days=7)
    while d <= newest:
        out.append(d)
        d += timedelta(days=7)
    return out


def discover_slugs(session: requests.Session, wanted: list[date]) -> dict[date, str]:
    """Find an unstored event slug for each wanted date in regulars' archives."""
    if not wanted:
        return {}
    seen = known_slugs()
    months = sorted({(d.year, d.month) for d in wanted})
    found: dict[date, str] = {}
    for username in regulars():
        for year, month in months:
            archive = get_json(session, ARCHIVE.format(username=username, year=year, month=month))
            time.sleep(PAUSE_SECONDS)
            for slug in slugs_in_archive(archive) - seen:
                d = slug_date(slug)
                if d in wanted and d not in found:
                    found[d] = slug
        if len(found) == len(wanted):
            break
    return found


def process(session: requests.Session, slug: str, expected: date | None = None) -> dict:
    """Load one finished event. Raises if it isn't a finished 1+0 Bullet Brawl."""
    details = get_json(session, API.format(slug=slug))
    if details is None:
        raise ValueError(f'{slug}: chess.com has no such tournament')
    settings = details.get('settings', {})
    if details.get('status') != 'finished':
        raise RuntimeError(f'{slug}: status is {details.get("status")!r}, not finished yet')
    if str(settings.get('time_control')) != '60':
        raise ValueError(f'{slug}: time control {settings.get("time_control")!r} is not 1+0')
    event_date = event_day(details.get('start_time'))
    if expected and event_date != expected.isoformat():
        raise ValueError(f'{slug}: started on {event_date}, expected {expected}')
    if date.fromisoformat(event_date).weekday() != BULLET_BRAWL.weekday:
        raise ValueError(f'{slug}: started on {event_date}, which is not a Saturday')

    payload = get_json(session, f'{API.format(slug=slug)}/1')
    time.sleep(PAUSE_SECONDS)
    if payload is None:
        parsed = fetch_standings_pages(session, details['url'], slug, event_date, PAUSE_SECONDS)
    else:
        parsed = parse_round(slug, event_date, payload)
    played = [r for r in parsed.rows if r['games'] > 0]
    if not played:
        raise RuntimeError(f'{slug}: no player has any games')

    with get_conn() as conn:
        write_event(conn, slug, event_date, details, parsed)
    summary = {'slug': slug, 'date': event_date, 'players': len(parsed.rows), 'played': len(played),
               'winner': next((r['username'] for r in parsed.rows if r['rank'] == 1), None),
               'points_mismatches': parsed.points_mismatches}
    print(f'  loaded {summary}')
    return summary


def enrich_new_players(min_appearances: int = MIN_APPEARANCES) -> int:
    """Add and enrich accounts with enough games to enter the model's pool.

    Display names are how Kalshi contracts are matched to predictions, so a
    player missing from player_information can't be traded.
    """
    with get_conn() as conn:
        eligible = [r[0] for r in conn.execute(
            f'SELECT username FROM bullet_brawl_standings WHERE games > 0 AND date >= ? '
            f'GROUP BY username HAVING COUNT(*) >= ?',
            (BULLET_BRAWL.model.data_cutoff, min_appearances),
        ).fetchall()]
        # Usernames are case-insensitive: an account on record under any
        # capitalization is not new.
        on_record = {r[0].lower() for r in conn.execute('SELECT username FROM player_information').fetchall()}
        new = [u for u in eligible if u.lower() not in on_record]
        conn.executemany('INSERT INTO player_information (username) VALUES (?)', [(u,) for u in new])
        conn.commit()
        enrich_players(conn, new)
    return len(new)


def run(slug_override: str | None = None) -> dict:
    """The weekly pipeline's scrape step."""
    session = requests.Session()
    session.headers.update(HEADERS)
    if slug_override:
        slug = slug_from_url(slug_override)
        print(f'  scrape_bb: slug override -> {slug}')
        loaded = [process(session, slug, slug_date(slug))]
        mode = 'override'
    else:
        wanted = missing_dates()
        print(f'  scrape_bb: looking for {[d.isoformat() for d in wanted]}')
        found = discover_slugs(session, wanted)
        if not found:
            raise ListingMissingSlugError(
                f'No new Bullet Brawl found in regulars\' game archives for {[d.isoformat() for d in wanted]}. '
                'Paste the event URL to load it directly.'
            )
        loaded = []
        for d in sorted(found):
            loaded.append(process(session, found[d], d))
        mode = 'archives'
    enriched = enrich_new_players()
    return {'mode': mode, 'events': loaded, 'players_enriched': enriched}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('slug', nargs='?', help='tournament URL or slug to load')
    ap.add_argument('--enrich-only', action='store_true', help='only add and enrich new players')
    args = ap.parse_args()
    if args.enrich_only:
        print(f'Enriched {enrich_new_players()} new player(s).')
        return
    print(run(args.slug))


if __name__ == '__main__':
    main()
