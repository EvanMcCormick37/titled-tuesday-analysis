"""
build_attendance_conflicts.py — Populate the attendance_conflicts table.

A conflict is a (TT date, player) pair where the player was in a concurrent
OTB broadcast whose earliest-start-UTC date lands on a Titled Tuesday.  These
weeks are excluded from EWMA participation-rate calculations in
src.data.load_and_prepare() so that p_participate reflects the player's
attendance rate on weeks where they had no unavoidable conflict.

The table is dropped and rebuilt from scratch on every run.

Usage:
    python scripts/build_attendance_conflicts.py
"""

import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH


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


def build_attendance_conflicts(conn: sqlite3.Connection) -> int:
    # 1. TT dates (YYYY-MM-DD)
    tt_dates = {row[0] for row in conn.execute(
        'SELECT DISTINCT date(date) FROM titled_tuesday_standings'
    ).fetchall()}

    # 2. norm(player_name) → (username, player_name), preferring is_default=1
    pi_rows = conn.execute(
        'SELECT username, player_name, is_default FROM player_information '
        'WHERE player_name IS NOT NULL'
    ).fetchall()
    name_to_player: dict[str, tuple[str, str]] = {}
    for uname, pname, is_def in pi_rows:
        n = _norm_name(pname)
        if not n:
            continue
        if n not in name_to_player or is_def:
            name_to_player[n] = (uname, pname)

    # 3. broadcast → set of TT-date rounds
    broadcast_tt_dates: dict[str, set[str]] = defaultdict(set)
    for bname, rdate in conn.execute(
        'SELECT broadcast_name, date(earliest_start_utc) FROM other_event_rounds'
    ):
        if rdate in tt_dates:
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

    # 5. Drop + recreate table
    conn.execute('DROP TABLE IF EXISTS attendance_conflicts')
    conn.execute(
        'CREATE TABLE attendance_conflicts ('
        '  date        TEXT NOT NULL, '
        '  player_name TEXT NOT NULL, '
        '  username    TEXT NOT NULL, '
        '  PRIMARY KEY (date, username)'
        ')'
    )
    conn.executemany(
        'INSERT INTO attendance_conflicts (date, player_name, username) VALUES (?, ?, ?)',
        sorted(conflicts),
    )
    conn.execute(
        'CREATE INDEX idx_attendance_conflicts_username ON attendance_conflicts(username)'
    )
    conn.commit()

    print(f'  Inserted {len(conflicts):,} conflict rows '
          f'({len(unmatched):,} broadcast names unmatched to player_information)')
    return len(conflicts)


def main() -> None:
    print(f'Building attendance_conflicts in {DB_PATH}...')
    conn = sqlite3.connect(DB_PATH)
    try:
        build_attendance_conflicts(conn)
        n_dates, n_users = conn.execute(
            'SELECT COUNT(DISTINCT date), COUNT(DISTINCT username) FROM attendance_conflicts'
        ).fetchone()
        print(f'  Distinct TT dates with any conflict: {n_dates}')
        print(f'  Distinct players with any conflict:  {n_users}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
