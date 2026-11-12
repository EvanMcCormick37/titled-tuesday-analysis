"""
build_attendance_conflicts.py — Populate the attendance_conflicts table.

A conflict is a (event date, player) pair where the player was in a concurrent
OTB broadcast whose earliest-start-UTC date lands on one of the market's event
dates.  These
weeks are excluded from EWMA participation-rate calculations in
src.data.load_and_prepare() so that p_participate reflects the player's
attendance rate on weeks where they had no unavoidable conflict.

Each run replaces only the given market's rows; defaults to Titled Tuesday.

Usage:
    python scripts/build_attendance_conflicts.py
    python scripts/build_attendance_conflicts.py --market bullet_brawl
"""

import argparse
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db import get_conn
from src.markets import SPECS, TITLED_TUESDAY, MarketSpec


def _norm_name(s: str) -> str:
    """'Last, First' → 'first last', strip diacritics, lowercase."""
    if not s:
        return ''
    if ',' in s:
        last, first = s.split(',', 1)
        s = f"{first.strip()} {last.strip()}"
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return ' '.join(s.lower().split())


def build_attendance_conflicts(conn, spec: MarketSpec = TITLED_TUESDAY) -> int:
    # 1. Event dates (YYYY-MM-DD)
    tt_dates = {str(row[0])[:10] for row in conn.execute(
        f'SELECT DISTINCT substr(CAST(date AS TEXT), 1, 10) FROM {spec.model.standings_table}'
    ).fetchall()}

    # 2. norm(player_name) → (username, player_name). A player's account seen most
    #    recently in this market wins, then is_default, so the conflict lands on the
    #    account whose attendance it corrects.
    latest = dict(conn.execute(
        f'SELECT username, MAX(CAST(date AS TEXT)) FROM {spec.model.standings_table} GROUP BY username'
    ).fetchall())
    pi_rows = conn.execute(
        'SELECT username, player_name, is_default FROM player_information '
        'WHERE player_name IS NOT NULL'
    ).fetchall()
    ranked: dict[str, tuple[tuple, str, str]] = {}
    for uname, pname, is_def in pi_rows:
        n = _norm_name(pname)
        if not n:
            continue
        key = (str(latest.get(uname) or ''), bool(is_def))
        if n not in ranked or key > ranked[n][0]:
            ranked[n] = (key, uname, pname)
    name_to_player = {n: (u, p) for n, (_, u, p) in ranked.items()}

    # 3. broadcast → set of TT-date rounds
    broadcast_tt_dates: dict[str, set[str]] = defaultdict(set)
    for bname, rdate in conn.execute(
        'SELECT broadcast_name, substr(CAST(earliest_start_utc AS TEXT), 1, 10) FROM other_event_rounds'
    ):
        rdate = str(rdate)[:10] if rdate is not None else None
        if rdate and rdate in tt_dates:
            broadcast_tt_dates[bname].add(rdate)

    # 4. For each participant in a conflict-carrying broadcast, emit a row per date
    conflicts: set[tuple[str, str, str]] = set()   # (date, player_name, username)
    unmatched: set[str] = set()
    for bname, raw_pname in conn.execute(
        'SELECT broadcast_name, player_name FROM other_event_participants'
    ):
        dates = broadcast_tt_dates.get(bname)
        if not dates:
            continue
        match = name_to_player.get(_norm_name(raw_pname))
        if not match:
            unmatched.add(raw_pname)
            continue
        username, player_name = match
        for d in dates:
            conflicts.add((d, player_name, username))

    # 5. Replace this market's rows
    conn.execute('DELETE FROM attendance_conflicts WHERE market = ?', (spec.slug,))
    conn.executemany(
        'INSERT INTO attendance_conflicts (market, date, player_name, username) VALUES (?, ?, ?, ?)',
        [(spec.slug, *row) for row in sorted(conflicts)],
    )
    conn.commit()

    print(f'  Inserted {len(conflicts):,} conflict rows '
          f'({len(unmatched):,} broadcast names unmatched to player_information)')
    return len(conflicts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--market', choices=sorted(SPECS), default=TITLED_TUESDAY.slug)
    spec = SPECS[ap.parse_args().market]
    print(f'Building attendance_conflicts for {spec.label}...')
    conn = get_conn()
    try:
        build_attendance_conflicts(conn, spec)
        n_dates, n_users = conn.execute(
            'SELECT COUNT(DISTINCT date), COUNT(DISTINCT username) FROM attendance_conflicts '
            'WHERE market = ?', (spec.slug,)
        ).fetchone()
        print(f'  Distinct event dates with any conflict: {n_dates}')
        print(f'  Distinct players with any conflict:  {n_users}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
