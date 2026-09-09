"""
One-time script: import Sept 8, 2026 TT standings from local JSON file.
Chess.com hasn't posted the tournament page yet, so results were extracted manually.
"""

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.config import DB_PATH
from scripts.scraping.update_titled_tuesday import _insert_new_players, enrich_players

JSON_PATH = PROJECT_ROOT / "data" / "2026-09-08-standings.json"
SLUG      = "titled-tuesday-blitz-september-08-2026-manual"
DATE      = "2026-09-08 00:00:00"
SESSION   = None


def main():
    players = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(players)} players from JSON")

    winner = next((p["username"] for p in players if p["rank"] == 1), None)
    print(f"Winner: {winner}")

    conn = sqlite3.connect(DB_PATH)

    # standings
    old_n = conn.execute(
        "SELECT COUNT(*) FROM titled_tuesday_standings WHERE tournament_slug = ?", (SLUG,)
    ).fetchone()[0]
    conn.execute("DELETE FROM titled_tuesday_standings WHERE tournament_slug = ?", (SLUG,))

    pd.DataFrame([{
        "date":            DATE,
        "tournament_slug": SLUG,
        "session":         SESSION,
        "rank":            p["rank"],
        "username":        p["username"],
        "title":           p.get("title"),
        "country":         p.get("country"),
        "rating":          p.get("rating"),
        "score":           p.get("score"),
        "tie_break":       p.get("tie_break"),
        "wins":            p.get("wins"),
        "draws":           p.get("draws"),
        "byes":            p.get("byes"),
    } for p in players]).to_sql("titled_tuesday_standings", conn, if_exists="append", index=False)
    print(f"standings: replaced {old_n} rows -> {len(players)} new")

    # tournaments
    existing = pd.read_sql_query(
        "SELECT * FROM titled_tuesday_tournaments WHERE tournament_slug = ?", conn, params=(SLUG,)
    )
    conn.execute("DELETE FROM titled_tuesday_tournaments WHERE tournament_slug = ?", (SLUG,))
    pd.DataFrame([{
        "date":            DATE,
        "time_local":      existing.iloc[0]["time_local"] if not existing.empty else None,
        "title":           "Titled-Tuesday-Blitz-September-08-2026-Manual",
        "session":         SESSION,
        "num_players":     len(players),
        "winner":          winner,
        "tournament_slug": SLUG,
        "url":             None,
    }]).to_sql("titled_tuesday_tournaments", conn, if_exists="append", index=False)
    print(f"tournaments: num_players={len(players)}, winner={winner}")

    conn.commit()

    all_usernames = [p["username"] for p in players if p.get("username")]
    new_usernames = _insert_new_players(conn, all_usernames)
    enrich_players(conn, new_usernames)

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
