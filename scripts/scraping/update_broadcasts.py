#!/usr/bin/env python3
"""Weekly Lichess broadcast pipeline — streaming, no filesystem cache.

Streams the current and previous month's PGN dumps from Lichess directly
through the headers-only parser into the DB. Older months are never
re-scraped — the one-time 2020→present backfill lives permanently in
other_events / other_event_rounds / other_event_participants, and only
the two most recent months get refreshed each week because Lichess
continues to append to the current month and finalises the previous
month a few days after it ends.

Flow:
  1. Stream `lichess_db_broadcast_YYYY-MM.pgn.zst` for each refresh month,
     parsing headers in-memory. No disk writes for the raw PGN.
  2. Upsert into other_events / other_event_rounds / other_event_participants
     (INSERT OR REPLACE, idempotent).
  3. Rebuild attendance_conflicts via build_attendance_conflicts().

Usage:
    python scripts/scraping/update_broadcasts.py
    python scripts/scraping/update_broadcasts.py --months 2    # how many trailing months to refresh (default 2)
"""
import argparse
import io
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import zstandard

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))

from src.config import DB_PATH
from build_attendance_conflicts import build_attendance_conflicts

BASE_URL = 'https://database.lichess.org/broadcast'
UA_HEADERS = {'User-Agent': 'tt-attendance-research (contact: e.kidmccorm@gmail.com)'}

_ONLINE_SITE_RE = re.compile(r'\.(com|org|net|io|tv|app)\b', re.I)
_ONLINE_NAME_RE = re.compile(
    r'\bchess\.com\b|\blichess\b|\bonline\b|\bspeed chess\b|\bchess24\b', re.I
)


# ── Month enumeration ─────────────────────────────────────────────────────────

def _recent_months(n: int) -> list[str]:
    """Return the last n months INCLUDING current one, oldest first."""
    t = date.today()
    y, m = t.year, t.month
    out = []
    for _ in range(n):
        out.append(f'{y:04d}-{m:02d}')
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


# ── PGN parsing (headers-only, streaming) ─────────────────────────────────────

def _iter_games_from_stream(raw_stream):
    """Yield header-dicts for each game in a .pgn.zst stream.

    raw_stream must be a readable binary file-like object (e.g. an open
    urllib HTTP response).
    """
    dctx = zstandard.ZstdDecompressor()
    reader = dctx.stream_reader(raw_stream)
    text = io.TextIOWrapper(reader, encoding='utf-8', errors='replace')
    tags, in_movetext = {}, False
    for line in text:
        line = line.strip()
        if line.startswith('[') and line.endswith(']') and '"' in line:
            if in_movetext and tags:
                yield tags
                tags, in_movetext = {}, False
            try:
                key, rest = line[1:-1].split(' ', 1)
                tags[key] = rest.strip().strip('"')
            except ValueError:
                pass
        elif line and tags:
            in_movetext = True
    if tags:
        yield tags


def _stream_month(ym: str):
    """Yield game-header dicts for the given month's Lichess PGN dump.

    Returns an empty iterator if the file 404s — Lichess publishes dumps
    lazily (the current month is updated continuously and the previous
    month is only finalised a few days after it ends), so a missing file
    for a recent month is expected, not an error.
    """
    name = f'lichess_db_broadcast_{ym}.pgn.zst'
    url  = f'{BASE_URL}/{name}'
    req  = urllib.request.Request(url, headers=UA_HEADERS)
    print(f'  streaming {name} ...')
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f'    [skip] {name} not yet published on Lichess (HTTP 404)')
            return
        raise
    with resp:
        yield from _iter_games_from_stream(resp)


def _to_utc(tags):
    d, t = tags.get('UTCDate'), tags.get('UTCTime')
    if not d:
        return None
    try:
        return datetime.strptime(f'{d} {t or "00:00:00"}', '%Y.%m.%d %H:%M:%S') \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _accumulate(months: list[str]) -> tuple[dict, dict, int]:
    ev  = defaultdict(lambda: {
        'players': set(), 'rounds': set(), 'tcs': Counter(),
        'first': None, 'last': None, 'n_games': 0,
        'titled': set(), 'sites': Counter(), 'fide_id_games': 0,
    })
    rnd = defaultdict(list)
    n_games = 0

    for ym in months:
        for tags in _stream_month(ym):
            white  = tags.get('White', '')
            black  = tags.get('Black', '')
            key    = tags.get('BroadcastName') or tags.get('Event', '')
            round_ = tags.get('Round', '')
            tc     = tags.get('TimeControl', '')
            dt     = _to_utc(tags)
            site   = tags.get('Site', '')

            e = ev[key]
            e['n_games'] += 1
            e['players'].update(x for x in (white, black) if x)
            if round_:
                e['rounds'].add(round_)
            if tc:
                e['tcs'][tc] += 1
            if dt:
                e['first'] = min(e['first'] or dt, dt)
                e['last']  = max(e['last']  or dt, dt)
                rnd[(key, round_)].append(dt)
            if site:
                e['sites'][site] += 1
            if tags.get('WhiteFideId') or tags.get('BlackFideId'):
                e['fide_id_games'] += 1
            for name, title_tag in [(white, 'WhiteTitle'), (black, 'BlackTitle')]:
                if name:
                    title = tags.get(title_tag, '')
                    if title and title != '?':
                        e['titled'].add(name)
            n_games += 1
    return dict(ev), dict(rnd), n_games


# ── DB upsert ─────────────────────────────────────────────────────────────────

def _upsert(conn: sqlite3.Connection, ev: dict, rnd: dict) -> None:
    """Merge the parsed window into other_events/_rounds/_participants.

    Events whose games span the refresh window and older months are handled
    correctly: we only touch the (broadcast_name) and (broadcast_name, round)
    keys that appear in the window; older months' events stay untouched.

    For an event that straddles the boundary, however, aggregates like
    first_game_utc / n_games are only correct for the window's view of the
    event. If an event's games span two months and the earlier month is NOT
    in the refresh window, those aggregates will reflect only the recent
    window. In practice broadcasts of interest conclude within a single
    month and this is a non-issue — but worth knowing when debugging an
    oddly-shaped `other_events` row.
    """
    existing = {row[1] for row in conn.execute('PRAGMA table_info(other_events)')}
    for col, dtype in [('pct_titled', 'REAL'), ('is_online', 'INTEGER'), ('pct_with_fide_id', 'REAL')]:
        if col not in existing:
            conn.execute(f'ALTER TABLE other_events ADD COLUMN {col} {dtype}')

    def _row(k, e):
        modal_site = e['sites'].most_common(1)[0][0] if e['sites'] else ''
        is_online  = int(
            bool(_ONLINE_SITE_RE.search(modal_site)) or
            bool(_ONLINE_NAME_RE.search(k or ''))
        )
        pct_fide = round(e['fide_id_games'] / e['n_games'], 4) if e['n_games'] else 0.0
        pct_titl = round(len(e['titled']) / len(e['players']), 4) if e['players'] else None
        return (
            k,
            e['first'].isoformat() if e['first'] else None,
            e['last'].isoformat()  if e['last']  else None,
            len(e['rounds']), e['n_games'], len(e['players']),
            e['tcs'].most_common(1)[0][0] if e['tcs'] else None,
            pct_titl, is_online, pct_fide,
        )

    conn.executemany(
        '''INSERT OR REPLACE INTO other_events
           (broadcast_name, first_game_utc, last_game_utc,
            n_rounds, n_games, n_players, modal_time_control,
            pct_titled, is_online, pct_with_fide_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        [_row(k, e) for k, e in ev.items()],
    )
    conn.executemany(
        '''INSERT OR REPLACE INTO other_event_rounds
           (broadcast_name, round, n_games, earliest_start_utc, median_start_utc)
           VALUES (?, ?, ?, ?, ?)''',
        [
            (k, r, len(dts),
             sorted(dts)[0].isoformat(),
             sorted(dts)[len(dts) // 2].isoformat())
            for (k, r), dts in rnd.items()
        ],
    )
    conn.executemany(
        'INSERT OR REPLACE INTO other_event_participants (broadcast_name, player_name) VALUES (?, ?)',
        [(k, player) for k, e in ev.items() for player in e['players']],
    )
    conn.commit()


# ── Public entry point ────────────────────────────────────────────────────────

def run(months: int = 2) -> dict:
    """Refresh the last `months` months of broadcast data; rebuild conflicts.

    Returns a summary dict suitable for stashing in `job_runs.summary`.
    """
    refresh = _recent_months(months)
    print(f'Streaming months: {refresh}')
    ev, rnd, n_games = _accumulate(refresh)
    print(f'  parsed {n_games:,} games -> {len(ev):,} broadcasts, {len(rnd):,} (broadcast, round) pairs')

    conn = sqlite3.connect(DB_PATH)
    try:
        print('Upserting into other_events / _rounds / _participants ...')
        _upsert(conn, ev, rnd)

        print('Rebuilding attendance_conflicts ...')
        n_conflicts = build_attendance_conflicts(conn)
        n_dates, n_users = conn.execute(
            'SELECT COUNT(DISTINCT date), COUNT(DISTINCT username) FROM attendance_conflicts'
        ).fetchone()
    finally:
        conn.close()

    return {
        'months_refreshed':    refresh,
        'games_parsed':        n_games,
        'broadcasts_upserted': len(ev),
        'rounds_upserted':     len(rnd),
        'conflicts_total':     n_conflicts,
        'conflict_dates':      n_dates,
        'conflict_players':    n_users,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--months', type=int, default=2,
                    help='how many trailing months to refresh (default 2)')
    args = ap.parse_args()
    summary = run(months=args.months)
    print(f'OK — {summary}')


if __name__ == '__main__':
    main()
