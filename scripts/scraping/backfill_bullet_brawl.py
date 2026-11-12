#!/usr/bin/env python3
"""Backfill Bullet Brawl final standings into the local SQLite database.

Event lists come from the yearly tables in chess.com's Bullet Brawl article
(2023-2025) and a saved copy of the 2026 results table. Most article links go to a
news story, which links to the event. Every candidate is confirmed against the
API's start time, so a result can only be stored under its own date.

Resumable: dates already loaded are skipped unless --refresh is given. Each date is
written to data/bullet_brawl_backfill_report.csv as loaded, suspect or missing.

Usage:
    python scripts/scraping/backfill_bullet_brawl.py
    python scripts/scraping/backfill_bullet_brawl.py --only 2023-01-28,2026-11-07
"""
import argparse
import csv
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.bullet_brawl_ingest import API, HEADERS, fetch_standings_pages, get_json, parse_round, write_event
from src.config import DATA_DIR
from src.db import get_conn, is_postgres

ARTICLE = 'https://www.chess.com/article/view/bullet-brawl'
TABLE_2026 = DATA_DIR / 'manual_artifacts' / '2026_bullet_brawl_results_table.html'
REPORT = DATA_DIR / 'bullet_brawl_backfill_report.csv'
# Must start with bullet-brawl: stories also link the separate Community Bullet Brawl.
ARENA_LINK = re.compile(r'/tournament/live/(?:arena/)?(bullet-brawl[A-Za-z0-9-]*-\d{5,})')
# Event slugs for dates whose published links are wrong. Still confirmed by start time.
SLUG_OVERRIDES: dict[str, str] = {}
PAUSE_SECONDS = 1.5
SMALL_FIELD = 40
ET = ZoneInfo('America/New_York')


def _iso(text: str) -> str:
    return pd.Timestamp(text).date().isoformat()


def article_events(session: requests.Session) -> list[dict]:
    response = session.get(ARTICLE, timeout=60)
    response.raise_for_status()
    events = []
    for details in BeautifulSoup(response.text, 'html.parser').find_all('details'):
        for row in details.find_all('tr')[1:]:
            link, cells = row.find('a'), row.find_all('td')
            if link is None or len(cells) < 2:
                continue
            events.append({'date': _iso(link.get_text(strip=True)), 'href': link['href'],
                           'winner': cells[1].get_text(' ', strip=True)})
    return events


def table_2026_events() -> list[dict]:
    raw = TABLE_2026.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('cp1252', errors='replace')
    events = []
    for row in BeautifulSoup(text, 'html.parser').find_all('tr'):
        link = row.find('a', href=ARENA_LINK)
        if link is None:
            continue
        # Cells after the date read: title | name | title | name ...
        parts = [p.strip() for p in row.get_text('|', strip=True).split('|')]
        date_text = next(p for p in parts if re.search(r'20\d\d', p))
        winner = parts[parts.index(date_text) + 2] if len(parts) > parts.index(date_text) + 2 else ''
        events.append({'date': _iso(date_text), 'href': link['href'], 'winner': winner})
    return events


def candidate_slugs(session: requests.Session, href: str) -> list[str]:
    direct = ARENA_LINK.search(href)
    if direct:
        return [direct.group(1)]
    response = session.get(href, timeout=60)
    time.sleep(PAUSE_SECONDS)
    if response.status_code != 200:
        return []
    return list(dict.fromkeys(ARENA_LINK.findall(response.text.replace('\\/', '/'))))


def event_day(start_time: int | None) -> str | None:
    if not start_time:
        return None
    return datetime.fromtimestamp(start_time, timezone.utc).astimezone(ET).date().isoformat()


def find_event(session: requests.Session, event: dict) -> tuple[str, dict] | None:
    override = SLUG_OVERRIDES.get(event['date'])
    for slug in [override] if override else candidate_slugs(session, event['href']):
        details = get_json(session, API.format(slug=slug))
        time.sleep(PAUSE_SECONDS)
        if details and event_day(details.get('start_time')) == event['date']:
            return slug, details
    return None


def _name_key(name: str) -> frozenset[str]:
    plain = ''.join(c for c in unicodedata.normalize('NFD', name or '') if unicodedata.category(c) != 'Mn')
    return frozenset(re.findall(r'[a-z]+', plain.lower()))


def _known_names(conn) -> dict[str, str]:
    return {u.lower(): n for u, n in conn.execute(
        'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL').fetchall()}


def _page_player_count(session: requests.Session, details: dict) -> int | None:
    response = session.get(details['url'], timeout=60, headers=HEADERS)
    time.sleep(PAUSE_SECONDS)
    match = re.search(r'([\d,]+)\s+players', response.text, re.I)
    return int(match.group(1).replace(',', '')) if match else None


def load_event(session, event: dict, names: dict[str, str]) -> dict:
    result = {'date': event['date'], 'listed_winner': event['winner'], 'status': 'missing',
              'slug': '', 'players': 0, 'games': 0, 'winner_username': '', 'winner_check': '',
              'source': 'round api', 'issues': ''}
    found = find_event(session, event)
    if found is None:
        result['issues'] = 'no event link matched this date'
        return result
    slug, details = found
    result['slug'] = slug

    payload = get_json(session, f'{API.format(slug=slug)}/1')
    time.sleep(PAUSE_SECONDS)
    if payload is None:
        result['source'] = 'standings pages'
        parsed = fetch_standings_pages(session, details['url'], slug, event['date'], PAUSE_SECONDS)
    else:
        parsed = parse_round(slug, event['date'], payload)
    if not parsed.rows:
        result['issues'] = 'round had no players'
        return result

    issues = []
    settings = details.get('settings', {})
    if str(settings.get('time_control')) != '60':
        issues.append(f'time control {settings.get("time_control")}')
    if details.get('status') != 'finished':
        issues.append(f'status {details.get("status")}')
    if len(details.get('rounds', [])) != 1:
        issues.append(f'{len(details.get("rounds", []))} rounds')
    if parsed.missing_rank:
        issues.append(f'{parsed.missing_rank} players without a rank')
    if parsed.points_mismatches > 0.02 * len(parsed.rows):
        issues.append(f'{parsed.points_mismatches}/{len(parsed.rows)} players have points that differ from their games')
    if len(parsed.rows) < SMALL_FIELD:
        on_page = _page_player_count(session, details)
        issues.append(f'small field: {len(parsed.rows)} stored, page says {on_page}')

    winner = next((r for r in parsed.rows if r['rank'] == 1), None)
    if winner:
        result['winner_username'] = winner['username']
        known = names.get(winner['username'].lower())
        if known is None:
            result['winner_check'] = 'unknown account'
        elif _name_key(known) & _name_key(event['winner']):
            result['winner_check'] = 'match'
        else:
            result['winner_check'] = 'mismatch'
            issues.append(f'winner mismatch: listed {event["winner"]}, standings {known}')

    with get_conn() as conn:
        write_event(conn, slug, event['date'], details, parsed)
    result.update(status='suspect' if issues else 'loaded', players=len(parsed.rows),
                  games=sum(r['games'] for r in parsed.rows) // 2, issues='; '.join(issues))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', help='comma-separated event dates')
    ap.add_argument('--refresh', action='store_true', help='reload dates already in the database')
    args = ap.parse_args()
    if is_postgres():
        raise SystemExit('This backfill writes to the local SQLite database. Unset DATABASE_URL.')

    session = requests.Session()
    session.headers.update(HEADERS)
    events = sorted(article_events(session) + table_2026_events(), key=lambda e: e['date'])
    if args.only:
        wanted = set(args.only.split(','))
        events = [e for e in events if e['date'] in wanted]

    with get_conn() as conn:
        names = _known_names(conn)
        loaded = {str(r[0])[:10] for r in conn.execute('SELECT date FROM bullet_brawl_tournaments').fetchall()}
    print(f'{len(events)} events listed; {len(loaded)} already loaded')

    results = []
    for i, event in enumerate(events, 1):
        if event['date'] in loaded and not args.refresh:
            continue
        try:
            result = load_event(session, event, names)
        except Exception as exc:  # noqa: BLE001 — one bad event must not stop the rest
            result = {'date': event['date'], 'listed_winner': event['winner'], 'status': 'missing', 'slug': '',
                      'players': 0, 'games': 0, 'winner_username': '', 'winner_check': '', 'source': '',
                      'issues': f'{type(exc).__name__}: {exc}'}
        results.append(result)
        print(f'[{i}/{len(events)}] {result["date"]} {result["status"]:<8} players={result["players"]:<4} '
              f'games={result["games"]:<5} winner={result["winner_username"] or "-":<22} {result["issues"]}', flush=True)

    if results:
        previous = []
        if REPORT.exists():
            with REPORT.open(encoding='utf-8', newline='') as fh:
                done = {r['date'] for r in results}
                previous = [r for r in csv.DictReader(fh) if r['date'] not in done]
        with REPORT.open('w', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(sorted(previous + results, key=lambda r: r['date']))

    counts = pd.Series([r['status'] for r in results]).value_counts().to_dict()
    print(f'Done: {counts}. Report: {REPORT}')


if __name__ == '__main__':
    main()
