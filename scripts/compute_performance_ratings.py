"""
Compute and store performance_rating for each titled_tuesday_standings row.

Formula:
    performance_rating = aroc_1 + 800 * (score / 11 - 0.5)

Denominator is always 11 (TT round count), so partial results (dropouts)
are penalised rather than inflated.

Rows with NULL aroc_1 are skipped. Runs idempotently — safe to re-run.
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH


def _ensure_column(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(titled_tuesday_standings)")}
    if 'performance_rating' not in existing:
        conn.execute("ALTER TABLE titled_tuesday_standings ADD COLUMN performance_rating REAL")
        conn.commit()


def compute(conn: sqlite3.Connection) -> tuple[int, int]:
    rows = conn.execute("""
        SELECT rowid, score, wins, draws, byes, aroc_1
        FROM titled_tuesday_standings
        WHERE aroc_1 IS NOT NULL
    """).fetchall()

    updated = skipped = 0
    batch = []
    for rowid, score, wins, draws, byes, aroc_1 in rows:
        if score is None:
            skipped += 1
            continue
        perf = aroc_1 + 800 * (score / 11 - 0.5)
        batch.append((round(perf, 1), rowid))
        updated += 1

    conn.executemany(
        "UPDATE titled_tuesday_standings SET performance_rating = ? WHERE rowid = ?",
        batch,
    )
    conn.commit()
    return updated, skipped


def show_sample(conn: sqlite3.Connection) -> None:
    print("\nTop 15 individual performances by performance_rating:")
    print(f"{'Username':<22} {'Tournament':<45} {'Score':>6} {'AROC1':>6} {'PerfRating':>10}")
    print("-" * 93)
    rows = conn.execute("""
        SELECT s.username, s.tournament_slug, s.score, s.aroc_1, s.performance_rating
        FROM titled_tuesday_standings s
        WHERE s.performance_rating IS NOT NULL
        ORDER BY s.performance_rating DESC
        LIMIT 15
    """).fetchall()
    for username, slug, score, aroc_1, perf in rows:
        print(f"{(username or '?'):<22} {slug:<45} {score:>6.1f} {aroc_1:>6.0f} {perf:>10.1f}")

    print("\nDistribution of performance_rating (rows with data):")
    stats = conn.execute("""
        SELECT
            COUNT(*)                            AS n,
            ROUND(MIN(performance_rating), 1)   AS min,
            ROUND(AVG(performance_rating), 1)   AS mean,
            ROUND(MAX(performance_rating), 1)   AS max
        FROM titled_tuesday_standings
        WHERE performance_rating IS NOT NULL
    """).fetchone()
    print(f"  n={stats[0]:,}  min={stats[1]}  mean={stats[2]}  max={stats[3]}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    _ensure_column(conn)
    updated, skipped = compute(conn)
    print(f"Updated {updated:,} rows  |  skipped {skipped} (zero rounds or null score)")
    show_sample(conn)
    conn.close()


if __name__ == '__main__':
    main()
