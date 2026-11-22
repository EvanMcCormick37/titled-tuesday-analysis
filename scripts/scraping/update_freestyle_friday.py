#!/usr/bin/env python3
"""Load new Freestyle Friday results, then enrich new regulars via the chess.com PubAPI.

Freestyle Friday is a regular chess.com live tournament (11-round Swiss, chess960,
3+2), so its standings pages have Titled Tuesday's layout and are parsed by the
same code (`update_titled_tuesday.fetch_standings`). The chess.com/events page for
it is rendered client-side and isn't read here.

Chess.com has no listing page for it either, so events are found the way Bullet
Brawl's are: in the monthly game archives of the event's most frequent recent
players, where every tournament game links its tournament. Each candidate is
confirmed against the public tournament API (finished, Swiss, chess960, started on
its slug's date, a Friday) before its standings are stored.

Works on SQLite and Postgres. Each event replaces its own rows, so a rerun is safe.

Usage:
    python scripts/scraping/update_freestyle_friday.py                 # discover and load new events
    python scripts/scraping/update_freestyle_friday.py <url-or-slug>   # load one event
    python scripts/scraping/update_freestyle_friday.py --backfill      # walk back to the first event
    python scripts/scraping/update_freestyle_friday.py --backfill --start 2026-06-01 --until 2025-01-01
    python scripts/scraping/update_freestyle_friday.py --enrich-only   # just fill in player names
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts' / 'scraping'))

import requests

from src.bullet_brawl_ingest import API, event_day, get_json
from src.config import MIN_APPEARANCES
from src.db import get_conn
from src.markets import FREESTYLE_FRIDAY
from src.multi_market_schema import FREESTYLE_FRIDAY_DDL
from update_titled_tuesday import ListingMissingSlugError, enrich_players, fetch_standings

SPEC = FREESTYLE_FRIDAY
STANDINGS = SPEC.model.standings_table
TOURNAMENTS = 'freestyle_friday_tournaments'
HEADERS = {'User-Agent': 'TT-attendance-research (personal project; contact via chess.com messages)'}
ARCHIVE = 'https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}'
PAUSE_SECONDS = 1.0
REGULARS = 15           # players whose archives are searched each week
RECENT_EVENTS = 8       # how far back "regular" looks
BACKFILL_REGULARS = 25  # archives searched per month when walking back
BACKFILL_EMPTY_MONTHS = 2   # stop after this many consecutive months with no event

# Anything before "freestyle-friday" is captured too, so a variant such as an
# "early-" session is noticed (and rejected by `process`) rather than silently missed.
EVENT_SLUG = re.compile(r'/tournament/(?:live/)?(([a-z-]*?)freestyle-friday-([a-z]+)-(\d{2})-(\d{4})-\d{5,})')
_MONTHS = {m: i for i, m in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'), start=1)}

STANDINGS_COLUMNS = ('date', 'tournament_slug', 'rank', 'username', 'title', 'rating', 'score',
                     'tie_break', 'aroc_1', 'performance_rating', 'wins', 'draws', 'byes')


# ── Slugs ────────────────────────────────────────────────────────────────────

def slug_from_url(url_or_slug: str) -> str:
    """Accept a chess.com tournament URL (live or API form) or a bare slug."""
    m = EVENT_SLUG.search(url_or_slug.strip().lower())
    if m:
        return m.group(1)
    bare = url_or_slug.strip().strip('/').rsplit('/', 1)[-1].split('?')[0].lower()
    if not re.fullmatch(SPEC.event_slug_pattern, bare):
        raise ValueError(f'Not a Freestyle Friday tournament URL or slug: {url_or_slug!r}')
    return bare


def slug_date(slug: str) -> date | None:
    """The date in the slug: freestyle-friday-november-20-2026-31235059 → 2026-11-20."""
    m = EVENT_SLUG.search(f'/tournament/{slug}')
    if not m or m.group(3) not in _MONTHS:
        return None
    return date(int(m.group(5)), _MONTHS[m.group(3)], int(m.group(4)))


def slugs_in_archive(archive: dict | None) -> set[str]:
    """Freestyle Friday slugs referenced by a player's monthly game archive."""
    found = set()
    for game in (archive or {}).get('games', []):
        m = EVENT_SLUG.search(game.get('tournament') or '')
        if m:
            found.add(m.group(1))
    return found


# ── Database ─────────────────────────────────────────────────────────────────

def ensure_tables() -> None:
    with get_conn() as conn:
        for stmt in FREESTYLE_FRIDAY_DDL:
            conn.execute(stmt)
        conn.commit()


def known_slugs() -> set[str]:
    with get_conn() as conn:
        return {r[0] for r in conn.execute(f'SELECT tournament_slug FROM {TOURNAMENTS}').fetchall()}


def latest_event_date() -> date | None:
    with get_conn() as conn:
        last = conn.execute(f'SELECT MAX(date) FROM {TOURNAMENTS}').fetchone()[0]
    return date.fromisoformat(str(last)[:10]) if last else None


def _frequent_players(table: str, newest_first: bool, limit: int, since: str | None = None) -> list[str]:
    """Players in the most of a window of events: the latest RECENT_EVENTS, or the earliest."""
    order = 'DESC' if newest_first else 'ASC'
    where = f"WHERE date >= '{since}'" if since else ''
    with get_conn() as conn:
        dates = [r[0] for r in conn.execute(
            f'SELECT DISTINCT date FROM {table} {where} ORDER BY date {order} LIMIT ?', (RECENT_EVENTS,)
        ).fetchall()]
        if not dates:
            return []
        marks = ', '.join('?' * len(dates))
        rows = conn.execute(
            f'SELECT username, COUNT(*) AS n, MIN(rank) AS best FROM {table} '
            f'WHERE date IN ({marks}) GROUP BY username ORDER BY n DESC, best ASC LIMIT ?',
            (*dates, limit),
        ).fetchall()
    return [r[0] for r in rows]


def regulars(limit: int = REGULARS, earliest: bool = False) -> list[str]:
    """Most frequent Freestyle Friday players, falling back to Titled Tuesday's
    regulars before any Freestyle Friday is stored."""
    found = _frequent_players(STANDINGS, newest_first=not earliest, limit=limit)
    if found:
        return found
    since = (date.today() - timedelta(days=90)).isoformat()
    return _frequent_players('titled_tuesday_standings', newest_first=True, limit=max(limit, 40), since=since)


def missing_dates(today: date | None = None) -> list[date]:
    """Event days after the latest stored one, up to the most recent."""
    last = latest_event_date()
    newest = SPEC.most_recent_event(today)
    if last is None:
        return [newest]
    out, d = [], last + timedelta(days=7)
    while d <= newest:
        out.append(d)
        d += timedelta(days=7)
    return out


# ── Discovery ────────────────────────────────────────────────────────────────

def _archive_slugs(session: requests.Session, usernames: list[str], year: int, month: int,
                   stop_when=None) -> set[str]:
    found: set[str] = set()
    for username in usernames:
        archive = get_json(session, ARCHIVE.format(username=username, year=year, month=month))
        time.sleep(PAUSE_SECONDS)
        found |= slugs_in_archive(archive)
        if stop_when and stop_when(found):
            break
    return found


def discover_slugs(session: requests.Session, wanted: list[date]) -> dict[date, str]:
    """Find an unstored event slug for each wanted date in regulars' archives."""
    if not wanted:
        return {}
    seen = known_slugs()
    found: dict[date, str] = {}
    for year, month in sorted({(d.year, d.month) for d in wanted}):
        month_wanted = {d for d in wanted if (d.year, d.month) == (year, month)}

        def have_all(slugs: set[str]) -> bool:
            return month_wanted <= {slug_date(s) for s in slugs - seen}

        for slug in sorted(_archive_slugs(session, regulars(), year, month, have_all) - seen):
            d = slug_date(slug)
            if d in month_wanted and d not in found:
                found[d] = slug
    return found


# ── Loading ──────────────────────────────────────────────────────────────────

def process(session: requests.Session, slug: str, expected: date | None = None) -> dict:
    """Load one finished event. Raises if it isn't a finished chess960 Swiss on a Friday."""
    details = get_json(session, API.format(slug=slug))
    if details is None:
        raise ValueError(f'{slug}: chess.com has no such tournament')
    settings = details.get('settings', {})
    if details.get('status') != 'finished':
        raise RuntimeError(f'{slug}: status is {details.get("status")!r}, not finished yet')
    if settings.get('type') != 'swiss' or settings.get('rules') != 'chess960':
        raise ValueError(f'{slug}: {settings.get("type")!r}/{settings.get("rules")!r} is not a chess960 Swiss')
    event_date = event_day(details.get('start_time'))
    if expected and event_date != expected.isoformat():
        raise ValueError(f'{slug}: started on {event_date}, expected {expected}')
    if date.fromisoformat(event_date).weekday() != SPEC.weekday:
        raise ValueError(f'{slug}: started on {event_date}, which is not a Friday')

    players = [p for p in fetch_standings(slug, headers=HEADERS) if p['username']]
    if not players:
        raise RuntimeError(f'{slug}: the standings page has no players')
    winner = next((p['username'] for p in players if p['rank'] == 1), None)
    stamp = f'{event_date} 00:00:00+00:00'
    rows = [(stamp, slug, p['rank'], p['username'], p['title'], p['rating'], p['score'], p['tie_break'],
             p['aroc_1'], p['performance_rating'], p['wins'], p['draws'], p['byes']) for p in players]

    with get_conn() as conn:
        conn.execute(f'DELETE FROM {STANDINGS} WHERE tournament_slug = ?', (slug,))
        conn.execute(f'DELETE FROM {TOURNAMENTS} WHERE tournament_slug = ?', (slug,))
        conn.execute(
            f'INSERT INTO {TOURNAMENTS} (tournament_slug, date, start_ts, finish_ts, num_players, winner, '
            f'url, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (slug, stamp, details.get('start_time'), details.get('finish_time'), len(players), winner,
             details.get('url'), datetime.now(timezone.utc).isoformat(timespec='seconds')),
        )
        marks = ', '.join('?' * len(STANDINGS_COLUMNS))
        conn.executemany(f'INSERT INTO {STANDINGS} ({", ".join(STANDINGS_COLUMNS)}) VALUES ({marks})', rows)
        conn.commit()
    summary = {'slug': slug, 'date': event_date, 'players': len(players), 'winner': winner}
    print(f'  loaded {summary}')
    return summary


def enrich_new_players(min_appearances: int = MIN_APPEARANCES) -> int:
    """Add and enrich accounts with enough appearances to enter the model's pool.

    Display names are how Kalshi contracts are matched to predictions, so a
    player missing from player_information can't be traded.
    """
    with get_conn() as conn:
        eligible = [r[0] for r in conn.execute(
            f'SELECT username FROM {STANDINGS} WHERE date >= ? GROUP BY username HAVING COUNT(*) >= ?',
            (SPEC.model.data_cutoff, min_appearances),
        ).fetchall()]
        on_record = {r[0].lower() for r in conn.execute('SELECT username FROM player_information').fetchall()}
        new = [u for u in eligible if u.lower() not in on_record]
        conn.executemany('INSERT INTO player_information (username) VALUES (?)', [(u,) for u in new])
        conn.commit()
        enrich_players(conn, new)
    return len(new)


def _load_all(session: requests.Session, slugs: set[str]) -> tuple[list[dict], list[str]]:
    loaded, skipped = [], []
    for slug in sorted(slugs, key=lambda s: slug_date(s) or date.min):
        for attempt in range(3):
            try:
                loaded.append(process(session, slug, slug_date(slug)))
                break
            except requests.RequestException as exc:    # chess.com's pages throw the odd 500
                if attempt == 2:
                    print(f'  skipped after 3 attempts: {exc}')
                    skipped.append(f'{slug}: {exc}')
                else:
                    time.sleep(15 * (attempt + 1))
            except (ValueError, RuntimeError) as exc:
                print(f'  skipped: {exc}')
                skipped.append(f'{slug}: {exc}')
                break
        time.sleep(PAUSE_SECONDS)
    return loaded, skipped


def backfill(session: requests.Session, until: date | None = None, start: date | None = None) -> dict:
    """Walk back month by month through regulars' archives, from `start` (default:
    this month).

    Regulars are taken from the earliest events loaded so far, so the search
    follows the field as it changes. With `until`, walks every month down to it,
    through empty ones (the series paused from July to September 2026, and
    between seasons). Without it, stops after BACKFILL_EMPTY_MONTHS empty months.
    """
    loaded, skipped = [], []
    first = start or date.today()
    year, month = first.year, first.month
    empty = 0
    while until or empty < BACKFILL_EMPTY_MONTHS:
        if until and (year, month) < (until.year, until.month):
            break
        print(f'{year}-{month:02d}:')
        found = _archive_slugs(session, regulars(BACKFILL_REGULARS, earliest=True), year, month)
        found = {s for s in found if (d := slug_date(s)) and (d.year, d.month) == (year, month)}
        new = found - known_slugs()
        print(f'  {len(found)} event(s) in archives, {len(new)} new')
        empty = 0 if found else empty + 1
        got, bad = _load_all(session, new)
        loaded += got
        skipped += bad
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return {'events': loaded, 'skipped': skipped, 'gaps': gaps()}


def gaps() -> list[str]:
    """Fridays between the first and latest stored events that have no event."""
    with get_conn() as conn:
        days = {str(r[0])[:10] for r in conn.execute(f'SELECT date FROM {TOURNAMENTS}').fetchall()}
    if not days:
        return []
    d, last, out = date.fromisoformat(min(days)), date.fromisoformat(max(days)), []
    while d <= last:
        if d.isoformat() not in days:
            out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def run(slug_override: str | None = None) -> dict:
    """The weekly pipeline's scrape step."""
    ensure_tables()
    session = _session()
    if slug_override:
        slug = slug_from_url(slug_override)
        print(f'  scrape_ff: slug override -> {slug}')
        loaded = [process(session, slug, slug_date(slug))]
        mode = 'override'
    else:
        wanted = missing_dates()
        if not wanted:
            # The latest event is already stored (a backfill, or a rerun after this
            # step succeeded): nothing to scrape, so the pipeline can carry on.
            print(f'  scrape_ff: up to date through {latest_event_date()}')
            return {'mode': 'up_to_date', 'events': [], 'players_enriched': enrich_new_players()}
        print(f'  scrape_ff: looking for {[d.isoformat() for d in wanted]}')
        found = discover_slugs(session, wanted)
        if not found:
            raise ListingMissingSlugError(
                f'No new Freestyle Friday found in regulars\' game archives for '
                f'{[d.isoformat() for d in wanted]}. Paste the event URL to load it directly.'
            )
        loaded = [process(session, found[d], d) for d in sorted(found)]
        mode = 'archives'
    enriched = enrich_new_players()
    return {'mode': mode, 'events': loaded, 'players_enriched': enriched}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('slug', nargs='?', help='tournament URL or slug to load')
    ap.add_argument('--backfill', action='store_true', help='walk back through archives to the first event')
    ap.add_argument('--start', type=date.fromisoformat,
                    help='with --backfill: begin at this month (YYYY-MM-DD; default this month)')
    ap.add_argument('--until', type=date.fromisoformat,
                    help='with --backfill: walk down to this month, through empty months')
    ap.add_argument('--enrich-only', action='store_true', help='only add and enrich new players')
    args = ap.parse_args()
    if args.enrich_only:
        ensure_tables()
        print(f'Enriched {enrich_new_players()} new player(s).')
        return
    if args.backfill:
        ensure_tables()
        result = backfill(_session(), args.until, args.start)
        result['players_enriched'] = enrich_new_players()
        print(f"Loaded {len(result['events'])} event(s); skipped {len(result['skipped'])}; "
              f"{len(result['gaps'])} Friday gap(s): {result['gaps']}")
        for s in result['skipped']:
            print(f'  skipped {s}')
        print(f"Enriched {result['players_enriched']} new player(s).")
        return
    print(run(args.slug))


if __name__ == '__main__':
    main()
