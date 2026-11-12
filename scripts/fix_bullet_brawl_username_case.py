#!/usr/bin/env python3
"""Give Bullet Brawl usernames chess.com's capitalization, matching Titled Tuesday.

The first backfill stored Bullet Brawl usernames in lowercase, and enrichment
then added lowercase copies of accounts player_information already had (e.g.
'hikaru' beside 'Hikaru'). This script, run once against the local SQLite file:

  1. merges player_information rows that differ only by case into one row,
     keeping the Titled Tuesday spelling (else a capitalized one) and filling its
     empty columns from the copies;
  2. renames a remaining lowercase-only row to the case chess.com shows in its
     Bullet Brawl games;
  3. rewrites bullet_brawl_standings.username and bullet_brawl_tournaments.winner
     to the spelling on record (src.bullet_brawl_ingest.known_usernames);
  4. clears Bullet Brawl's local prediction and conflict rows, which are keyed by
     the old usernames. Rerun the pipeline steps afterwards.

Idempotent. The two pre-existing Titled Tuesday case pairs are left alone.

Usage:
    python scripts/fix_bullet_brawl_username_case.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.bullet_brawl_ingest import known_usernames
from src.db import get_conn, is_postgres, table_columns


def _tt_latest(conn) -> dict[str, str]:
    return {u: str(d) for u, d in conn.execute(
        'SELECT username, MAX(date) FROM titled_tuesday_standings GROUP BY username').fetchall()}


def _bb_display_case(conn) -> dict[str, str]:
    """lowercase username → the spelling in its most recent Bullet Brawl games."""
    out: dict[str, str] = {}
    for name, _ in conn.execute(
            'SELECT display_name, MAX(date) FROM bullet_brawl_standings WHERE games > 0 '
            'GROUP BY display_name ORDER BY MAX(date)').fetchall():
        out[name.lower()] = name
    return out


def merge_player_information(conn) -> tuple[int, int]:
    cols = [c for c in table_columns(conn, 'player_information') if c != 'username']
    rows = conn.execute(f'SELECT username, {", ".join(cols)} FROM player_information').fetchall()
    tt = _tt_latest(conn)
    groups: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        groups[row[0].lower()].append(row)

    merged = 0
    for group in (g for g in groups.values() if len(g) > 1):
        if sum(r[0] in tt for r in group) > 1:
            continue    # both spellings have Titled Tuesday history: not a copy
        keep = max(group, key=lambda r: (tt.get(r[0], ''), r[0] != r[0].lower(), r[0]))
        values = list(keep[1:])
        for other in group:
            if other is keep:
                continue
            values = [v if v is not None else o for v, o in zip(values, other[1:])]
            conn.execute('DELETE FROM player_information WHERE username = ?', (other[0],))
            merged += 1
        if 'is_default' in cols:
            values[cols.index('is_default')] = max(r[1 + cols.index('is_default')] or 0 for r in group)
        conn.execute(f'UPDATE player_information SET {", ".join(f"{c} = ?" for c in cols)} WHERE username = ?',
                     (*values, keep[0]))

    display = _bb_display_case(conn)
    renamed = 0
    for (u,) in conn.execute('SELECT username FROM player_information').fetchall():
        target = display.get(u.lower())
        if u == u.lower() and target and target != u and u not in tt:
            conn.execute('UPDATE player_information SET username = ? WHERE username = ?', (target, u))
            renamed += 1
    return merged, renamed


def rewrite_bullet_brawl(conn) -> int:
    before = set(conn.execute('SELECT tournament_slug, username FROM bullet_brawl_standings').fetchall())
    # Start from each row's own chess.com spelling, then unify on the one on record.
    conn.execute('UPDATE bullet_brawl_standings SET username = display_name '
                 'WHERE display_name IS NOT NULL AND lower(display_name) = lower(username)')
    known = known_usernames(conn)
    for (u,) in conn.execute('SELECT DISTINCT username FROM bullet_brawl_standings').fetchall():
        target = known.get(u.lower(), u)
        if target != u:
            conn.execute('UPDATE bullet_brawl_standings SET username = ? WHERE username = ?', (target, u))
    for slug, winner in conn.execute('SELECT tournament_slug, winner FROM bullet_brawl_tournaments '
                                     'WHERE winner IS NOT NULL').fetchall():
        conn.execute('UPDATE bullet_brawl_tournaments SET winner = ? WHERE tournament_slug = ?',
                     (known.get(winner.lower(), winner), slug))
    after = set(conn.execute('SELECT tournament_slug, username FROM bullet_brawl_standings').fetchall())
    return len({u for _, u in before - after})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='report the changes, then roll back')
    args = ap.parse_args()
    if is_postgres():
        raise SystemExit('This fixes the local SQLite file. Unset DATABASE_URL.')

    conn = get_conn()
    try:
        before = conn.execute('SELECT COUNT(*) FROM (SELECT 1 FROM player_information '
                              'GROUP BY lower(username) HAVING COUNT(*) > 1)').fetchone()[0]
        merged, renamed = merge_player_information(conn)
        changed = rewrite_bullet_brawl(conn)
        cleared = 0
        for table in ('latest_model_predictions_raw', 'latest_model_predictions', 'attendance_conflicts'):
            cleared += conn.execute(f"DELETE FROM {table} WHERE market = 'bullet_brawl'").rowcount
        after = conn.execute('SELECT COUNT(*) FROM (SELECT 1 FROM player_information '
                             'GROUP BY lower(username) HAVING COUNT(*) > 1)').fetchone()[0]
        print(f'player_information: {before} case-duplicate groups -> {after}; '
              f'{merged} copies merged away, {renamed} lowercase rows given their chess.com case')
        print(f'bullet_brawl_standings: {changed} usernames respelled; {cleared} stale Bullet Brawl '
              'prediction/conflict rows cleared')
        if args.dry_run:
            conn.rollback()
            print('Dry run: rolled back.')
        else:
            conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    main()
