#!/usr/bin/env python3
"""Weekly Lichess broadcast pipeline.

Steps:
  1. Fetch monthly PGN dumps from https://database.lichess.org/broadcast/
     - Force-redownload the last N months (Lichess updates the current-month
       file continuously and finalises last month a few days after it ends).
     - Download any older months that are missing locally.
  2. Re-parse ALL local dumps to keep other_events aggregates (n_games,
     first/last utc, n_players, ...) correct for events whose games straddle
     month boundaries.  Parsing is stream-based and headers-only so a full
     6-year backlog runs in a couple of minutes.
  3. Upsert into other_events / other_event_rounds / other_event_participants
     (INSERT OR REPLACE, idempotent).
  4. Rebuild the attendance_conflicts table via
     scripts.build_attendance_conflicts.build_attendance_conflicts().

Usage:
    python scripts/scraping/update_broadcasts.py                 # weekly refresh
    python scripts/scraping/update_broadcasts.py --refresh-recent 3
    python scripts/scraping/update_broadcasts.py --full-rebuild  # start-of-time
"""
import argparse
import io
import re
import sqlite3
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import zstandard

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))

from src.config import DATA_DIR, DB_PATH
from build_attendance_conflicts import build_attendance_conflicts

BASE_URL    = 'https://database.lichess.org/broadcast'
DUMPS_DIR   = DATA_DIR / 'lichess_dumps'
FIRST_MONTH = '2020-01'

_ONLINE_SITE_RE = re.compile(r'\.(com|org|net|io|tv|app)\b', re.I)
_ONLINE_NAME_RE = re.compile(
    r'\bchess\.com\b|\blichess\b|\bonline\b|\bspeed chess\b|\bchess24\b', re.I
)


# ── Month enumeration ─────────────────────────────────────────────────────────

def _month_range(start: str, end: str):
    y, m = map(int, start.split('-'))
    ye, me = map(int, end.split('-'))
    while (y, m) <= (ye, me):
        yield f'{y:04d}-{m:02d}'
        m += 1
        if m == 13:
            y, m = y + 1, 1


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


# ── Download ──────────────────────────────────────────────────────────────────

def _download_month(ym: str, dumps_dir: Path, force: bool) -> Path | None:
    name = f'lichess_db_broadcast_{ym}.pgn.zst'
    dest = dumps_dir / name
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    tmp = dest.with_suffix(dest.suffix + '.part')
    url = f'{BASE_URL}/{name}'
    tag = 'refresh' if dest.exists() else 'new'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'tt-attendance-research'})
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(dest)
        print(f'  [{tag}] {name} ({dest.stat().st_size/1e6:.1f} MB)')
        return dest
    except Exception as e:
        print(f'  FAILED {url}: {e}', file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return dest if dest.exists() else None


# ── PGN parsing (headers-only, streaming) ─────────────────────────────────────

def _iter_games(path: Path):
    dctx   = zstandard.ZstdDecompressor()
    opener = (lambda p: dctx.stream_reader(open(p, 'rb'))) if path.suffix == '.zst' \
        else (lambda p: open(p, 'rb'))
    with opener(path) as raw:
        text = io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
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


def _to_utc(tags):
    d, t = tags.get('UTCDate'), tags.get('UTCTime')
    if not d:
        return None
    try:
        return datetime.strptime(f'{d} {t or "00:00:00"}', '%Y.%m.%d %H:%M:%S') \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_all(paths: list[Path]) -> tuple[dict, dict, int]:
    ev  = defaultdict(lambda: {
        'players': set(), 'rounds': set(), 'tcs': Counter(),
        'first': None, 'last': None, 'n_games': 0,
        'titled': set(), 'sites': Counter(), 'fide_id_games': 0,
    })
    rnd = defaultdict(list)
    n_games = 0

    for p in paths:
        print(f'  parsing {p.name} ...')
        for tags in _iter_games(p):
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--refresh-recent', type=int, default=2,
                    help='force re-download the last N months (default 2)')
    ap.add_argument('--dumps-dir', default=str(DUMPS_DIR),
                    help='where to store .pgn.zst files (default: data/lichess_dumps)')
    ap.add_argument('--full-rebuild', action='store_true',
                    help=f'download every missing month since {FIRST_MONTH}')
    args = ap.parse_args()

    dumps_dir = Path(args.dumps_dir)
    dumps_dir.mkdir(parents=True, exist_ok=True)

    # Always sweep FIRST_MONTH -> current so we backfill any missing month,
    # then force-redownload the last N months (Lichess updates these live).
    # --full-rebuild upgrades the "force" set to include every month.
    end_month   = _recent_months(1)[0]
    all_months  = list(_month_range(FIRST_MONTH, end_month))
    refresh_set = set(all_months) if args.full_rebuild \
                                  else set(_recent_months(args.refresh_recent))

    # 1. Download
    print(f'Step 1/4: fetching dumps ({FIRST_MONTH} -> {end_month}); '
          f'force-refreshing {sorted(refresh_set & set(all_months))}')
    for ym in all_months:
        _download_month(ym, dumps_dir, force=(ym in refresh_set))

    # 2. Parse ALL local dumps to keep event aggregates correct across months
    all_local = sorted(dumps_dir.glob('lichess_db_broadcast_*.pgn.zst'))
    print(f'\nStep 2/4: parsing all {len(all_local)} local dumps')
    ev, rnd, n_games = _parse_all(all_local)
    print(f'  {n_games:,} games -> {len(ev):,} broadcasts, {len(rnd):,} (broadcast, round) pairs')

    conn = sqlite3.connect(DB_PATH)
    try:
        # 3. Upsert
        print(f'\nStep 3/4: upserting into other_events / _rounds / _participants')
        _upsert(conn, ev, rnd)
        print('  done')

        # 4. Rebuild attendance_conflicts
        print(f'\nStep 4/4: rebuilding attendance_conflicts')
        build_attendance_conflicts(conn)
        n_dates, n_users = conn.execute(
            'SELECT COUNT(DISTINCT date), COUNT(DISTINCT username) FROM attendance_conflicts'
        ).fetchone()
        print(f'  {n_dates} TT dates with conflicts across {n_users} players')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
