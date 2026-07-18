#!/usr/bin/env python3
"""Parse Lichess broadcast .pgn.zst dumps into schedule and participant tables.

Reads PGN headers only (never parses movetext), streaming, so a full month
parses in seconds with tiny memory use.

Writes to DB tables
-------------------
  other_events              one row per broadcast tournament
  other_event_rounds        one row per (event, round)
  other_event_participants  one row per (event, player)

Uses INSERT OR REPLACE so re-running on the same files is safe.

Usage:
    python scripts/parse_lichess_broadcasts.py dumps/*.pgn.zst
    python scripts/parse_lichess_broadcasts.py dumps/*.pgn.zst --db path/to/other.db
"""
import argparse
import io
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH

import zstandard


def iter_games(path: Path):
    """Yield one dict of PGN tag headers per game, streaming."""
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


def to_utc(tags):
    d, t = tags.get('UTCDate'), tags.get('UTCTime')
    if not d:
        return None
    try:
        return datetime.strptime(f'{d} {t or "00:00:00"}', '%Y.%m.%d %H:%M:%S') \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='+', help='.pgn.zst (or .pgn) files')
    ap.add_argument('--db', default=None,
                    help='Path to SQLite DB (default: project data/titled_tuesday.db)')
    args = ap.parse_args()

    db_path = args.db or DB_PATH

    ev  = defaultdict(lambda: {'players': set(), 'rounds': set(), 'tcs': Counter(),
                               'first': None, 'last': None, 'n_games': 0})
    rnd = defaultdict(list)   # (broadcast_name, round) -> [datetimes]
    n   = 0

    for p in map(Path, args.inputs):
        print(f'parsing {p.name} ...')
        for tags in iter_games(p):
            white  = tags.get('White', '')
            black  = tags.get('Black', '')
            key    = tags.get('BroadcastName', '') or tags.get('Event', '')
            round_ = tags.get('Round', '')
            tc     = tags.get('TimeControl', '')
            dt     = to_utc(tags)

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
            n += 1

    conn = sqlite3.connect(db_path)

    # other_events
    conn.executemany(
        '''INSERT OR REPLACE INTO other_events
           (broadcast_name, first_game_utc, last_game_utc,
            n_rounds, n_games, n_players, modal_time_control)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        [
            (k,
             e['first'].isoformat() if e['first'] else None,
             e['last'].isoformat()  if e['last']  else None,
             len(e['rounds']), e['n_games'], len(e['players']),
             e['tcs'].most_common(1)[0][0] if e['tcs'] else None)
            for k, e in ev.items()
        ],
    )

    # other_event_rounds
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

    # other_event_participants
    conn.executemany(
        'INSERT OR REPLACE INTO other_event_participants (broadcast_name, player_name) VALUES (?, ?)',
        [
            (k, player)
            for k, e in ev.items()
            for player in e['players']
        ],
    )

    conn.commit()
    conn.close()
    print(f'parsed {n} games across {len(ev)} broadcast tournaments -> {db_path}')


if __name__ == '__main__':
    main()
