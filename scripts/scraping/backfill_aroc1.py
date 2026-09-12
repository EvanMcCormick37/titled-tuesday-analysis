"""
Backfill aroc_1 (TB7 / AROC 1 tiebreak) into existing titled_tuesday_standings rows.

Scrapes each tournament page and updates only the aroc_1 column for matching
(tournament_slug, username) pairs. Skips tournaments where all rows already
have aroc_1 populated. Tournaments without a TB7 column (older events) will
have 0 rows updated and are not retried on subsequent runs.

Usage:
    python scripts/scraping/backfill_aroc1.py              # all slugs with NULL aroc_1
    python scripts/scraping/backfill_aroc1.py --force      # all slugs regardless
    python scripts/scraping/backfill_aroc1.py <slug>       # one specific slug
"""

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH

# Import fetch_standings and _ensure_aroc_column from the sibling script.
_ut_path = Path(__file__).parent / "update_titled_tuesday.py"
_spec = importlib.util.spec_from_file_location("update_titled_tuesday", _ut_path)
_ut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ut)

fetch_standings = _ut.fetch_standings
_ensure_aroc_column = _ut._ensure_aroc_column


def slugs_needing_backfill(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT DISTINCT tournament_slug
        FROM titled_tuesday_standings
        WHERE aroc_1 IS NULL
        ORDER BY date DESC
    """).fetchall()
    return [r[0] for r in rows]


def all_slugs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT DISTINCT tournament_slug
        FROM titled_tuesday_standings
        ORDER BY date DESC
    """).fetchall()
    return [r[0] for r in rows]


def backfill_slug(conn: sqlite3.Connection, slug: str) -> int:
    players = fetch_standings(slug)
    updated = 0
    for p in players:
        if p['aroc_1'] is None:
            continue
        cur = conn.execute(
            "UPDATE titled_tuesday_standings SET aroc_1 = ? WHERE tournament_slug = ? AND username = ?",
            (p['aroc_1'], slug, p['username']),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def main() -> None:
    force = '--force' in sys.argv
    slugs_arg = [a for a in sys.argv[1:] if not a.startswith('--')]

    conn = sqlite3.connect(DB_PATH)
    _ensure_aroc_column(conn)

    if slugs_arg:
        slugs = slugs_arg
    elif force:
        slugs = all_slugs(conn)
    else:
        slugs = slugs_needing_backfill(conn)

    if not slugs:
        print('Nothing to backfill — all rows already have aroc_1.')
        conn.close()
        return

    print(f'Backfilling {len(slugs)} tournament(s)…')
    for i, slug in enumerate(slugs, 1):
        print(f'[{i}/{len(slugs)}] {slug}', end='', flush=True)
        try:
            n = backfill_slug(conn, slug)
            print(f' → {n} rows updated')
        except Exception as e:
            print(f' → ERROR: {e}', file=sys.stderr)
        if i < len(slugs):
            time.sleep(2.0)

    conn.close()
    print('Done.')


if __name__ == '__main__':
    main()
