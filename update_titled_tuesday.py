"""
Update the Titled Tuesday standings/tournaments CSVs with new events.

Uses Chess.com's free, no-auth Published-Data API (PubAPI):
    https://api.chess.com/pub/tournament/{url-id}

Recent Titled Tuesday URL IDs follow a predictable pattern:
    titled-tuesday-blitz-{month}-{dd}-{yyyy}-{numeric-id}
The numeric id is not predictable, so we discover new events by scraping
the completed-tournaments listing page, then pull full standings from the API.

Usage:
    pip install requests beautifulsoup4
    python update_titled_tuesday.py

It appends any tournaments not already present in titled_tuesday_tournaments.csv
(matched by slug) and their standings to titled_tuesday_standings.csv.
Be polite: the PubAPI asks for serial (non-parallel) requests. This script
sleeps between calls and sends a User-Agent header (recommended by Chess.com).
"""

import csv
import re
import time
import datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
TOURN_CSV = HERE / "data/titled_tuesday_tournaments.csv"
STAND_CSV = HERE / "data/titled_tuesday_standings.csv"

HEADERS = {"User-Agent": "titled-tuesday-research (contact: your-email@example.com)"}
LISTING = "https://www.chess.com/tournament/live/titled-tuesdays"
API = "https://api.chess.com/pub/tournament/{slug}"

TT_SLUG_RE = re.compile(r"/tournament/live/((?:early-|late-)?titled-tuesday-blitz-[a-z]+-\d{2}-\d{4}-\d+)")


def known_slugs() -> set:
    with open(TOURN_CSV, newline="", encoding="utf-8") as f:
        return {row["slug"] for row in csv.DictReader(f)}


def discover_slugs(max_pages: int = 5) -> list:
    """Scrape the completed Titled Tuesday listing pages for tournament slugs."""
    slugs = []
    
    for page in range(1, max_pages + 1):
        url = LISTING if page == 1 else f"{LISTING}?page={page}"
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        found = TT_SLUG_RE.findall(resp.text)
        if not found:
            break
        slugs.extend(found)
        time.sleep(1.0)
    # keep order, drop dupes
    seen, ordered = set(), []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def parse_date_from_slug(slug: str):
    m = re.search(r"([a-z]+)-(\d{2})-(\d{4})", slug)
    if not m:
        return None
    month, day, year = m.groups()
    try:
        return dt.datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
    except ValueError:
        return None


def fetch_tournament(slug: str) -> dict:
    resp = requests.get(API.format(slug=slug), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_round_standings(slug: str, n_rounds: int) -> list:
    """Standings after the final round = final standings."""
    url = API.format(slug=slug) + f"/{n_rounds}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    players = data.get("players", [])
    # Sort by points desc, tie_break desc to assign ranks
    players.sort(key=lambda p: (-float(p.get("points", 0) or 0),
                                -float(p.get("tie_break", 0) or 0)))
    return players


def main():
    known = known_slugs()
    new = [s for s in discover_slugs() if s not in known]
    if not new:
        print("No new tournaments found — CSVs are up to date.")
        return
    print(f"Found {len(new)} new tournament(s): {new}")

    tourn_rows, stand_rows = [], []
    for slug in new:
        info = fetch_tournament(slug)
        time.sleep(1.0)
        n_rounds = len(info.get("rounds", []))
        session = "early" if slug.startswith("early-") else "late" if slug.startswith("late-") else ""
        date = parse_date_from_slug(slug)
        players = fetch_round_standings(slug, n_rounds)
        time.sleep(1.0)

        finished = [p for p in players if p.get("points") is not None]
        winner = finished[0]["username"] if finished else ""
        tourn_rows.append({
            "date": date.isoformat() if date else "",
            "time_local": "",
            "title": info.get("name", slug),
            "session": session,
            "num_players": len(players),
            "winner": winner,
            "slug": slug,
            "url": f"https://www.chess.com/tournament/live/{slug}",
        })
        for rank, p in enumerate(finished, start=1):
            stand_rows.append({
                "date": date.isoformat() if date else "",
                "tournament_slug": slug,
                "session": session,
                "rank": rank,
                "username": p.get("username", ""),
                "title": "",       # PubAPI standings do not include title/country/rating;
                "country": "",     # join against /pub/player/{username} if you need them
                "rating": "",
                "score": p.get("points", ""),
                "tie_break": p.get("tie_break", ""),
                "wins": "", "draws": "", "byes": "",
            })
        print(f"  {slug}: {len(finished)} players, winner={winner}")

    with open(TOURN_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "time_local", "title", "session",
                                          "num_players", "winner", "slug", "url"])
        w.writerows(tourn_rows)
    with open(STAND_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "tournament_slug", "session", "rank",
                                          "username", "title", "country", "rating",
                                          "score", "tie_break", "wins", "draws", "byes"])
        w.writerows(stand_rows)
    print("CSVs updated.")


if __name__ == "__main__":
    main()
