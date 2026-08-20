"""
Update Titled Tuesday standings and tournaments in the SQLite DB with new events,
then enrich any new player usernames via the chess.com PubAPI.

Scrapes Chess.com tournament standings pages directly using BeautifulSoup,
paginating through all player pages:
    https://www.chess.com/tournament/live/{slug}?&players={page}

Recent Titled Tuesday URL IDs follow a predictable pattern:
    titled-tuesday-blitz-{month}-{dd}-{yyyy}-{numeric-id}
The numeric id is not predictable, so we discover new events by scraping
the completed-tournaments listing page.

Usage:
    python scripts/update_titled_tuesday.py          # process all new slugs
    python scripts/update_titled_tuesday.py <slug>   # process one specific slug

Be polite: sleeps between requests and sends a User-Agent header.
"""

import json
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.config import DB_PATH

HEADERS   = {'User-Agent': 'titled-tuesday-research (contact: e.kidmccorm@gmail.com)'}
LISTING   = 'https://www.chess.com/tournament/live/titled-tuesdays'
TOURN_URL = 'https://www.chess.com/tournament/live/{slug}?&players={page}'

TT_SLUG_RE = re.compile(
    r'/tournament/live/((?:early-|late-)?titled-tuesday-blitz-[a-z]+-\d{2}-\d{4}-\d+)'
)

MONTH_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# ── Chess.com PubAPI enrichment ────────────────────────────────────────────────

_API = "https://api.chess.com/pub/player"
_UA  = {"User-Agent": "TT-attendance-research (personal project; contact via chess.com messages)"}

_ENRICH_NEW_COLS = {
    "status":               "TEXT",
    "chess_com_blitz_best": "INTEGER",
    "profile_url":          "TEXT",
    "fetch_error":          "TEXT",
}

_tls         = threading.local()
_pause_until = 0.0
_pause_lock  = threading.Lock()


def _session() -> requests.Session:
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
        _tls.s.headers.update(_UA)
    return _tls.s


def _backoff(seconds: float):
    global _pause_until
    with _pause_lock:
        _pause_until = max(_pause_until, time.time() + seconds)


def _wait_if_paused():
    while True:
        delta = _pause_until - time.time()
        if delta <= 0:
            return
        time.sleep(min(delta, 1.0))


def _get_json(url: str, cache: Path) -> dict | None:
    key = cache / (re.sub(r"[^A-Za-z0-9._-]", "_", url.split("/pub/")[-1]) + ".json")
    if key.exists():
        return json.loads(key.read_text() or "null")
    for attempt in range(6):
        _wait_if_paused()
        try:
            r = _session().get(url, timeout=30)
            if r.status_code == 404:
                key.write_text("null")
                return None
            if r.status_code in (429, 403):
                wait = 15 * (attempt + 1)
                print(f"    HTTP {r.status_code}, global backoff {wait}s", file=sys.stderr)
                _backoff(wait)
                continue
            r.raise_for_status()
            data = r.json()
            key.write_text(json.dumps(data))
            return data
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(3 * (attempt + 1))
    return None


def _enrich_one(username: str, cache: Path) -> dict:
    time.sleep(random.uniform(0, 1.0))
    row = {"username": username, "name": "", "title": "", "country": "",
           "status": "", "fide": "", "blitz_last": "", "blitz_best": "",
           "profile_url": "", "error": ""}
    prof = _get_json(f"{_API}/{username}", cache)
    if prof is None:
        row["error"] = "not_found_or_uncached"
        return row
    row["name"]        = prof.get("name", "")
    row["title"]       = prof.get("title", "")
    row["country"]     = (prof.get("country") or "").rsplit("/", 1)[-1]
    row["status"]      = prof.get("status", "")
    row["profile_url"] = prof.get("url", "")
    stats    = _get_json(f"{_API}/{username}/stats", cache) or {}
    fide_val = stats.get("fide", "")
    row["fide"] = fide_val if (isinstance(fide_val, int) and fide_val <= 2882) else ""
    try:
        row["blitz_last"] = stats["chess_blitz"]["last"]["rating"]
    except (KeyError, TypeError):
        pass
    try:
        row["blitz_best"] = stats["chess_blitz"]["best"]["rating"]
    except (KeyError, TypeError):
        pass
    return row


def _ensure_enrich_columns(conn: sqlite3.Connection):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(player_information)")}
    for col, dtype in _ENRICH_NEW_COLS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE player_information ADD COLUMN {col} {dtype}")
    conn.commit()


def _write_enriched_row(conn: sqlite3.Connection, row: dict):
    def _v(val):
        return val if val not in ("", None) else None

    conn.execute(
        """
        UPDATE player_information SET
            player_name            = COALESCE(player_name, ?),
            title                  = ?,
            country                = ?,
            status                 = ?,
            fide_rating            = ?,
            chess_com_blitz_rating = ?,
            chess_com_blitz_best   = ?,
            profile_url            = ?,
            fetch_error            = ?
        WHERE username = ?
        """,
        [
            _v(row.get("name")),
            _v(row.get("title")),
            _v(row.get("country")),
            _v(row.get("status")),
            _v(row.get("fide")),
            _v(row.get("blitz_last")),
            _v(row.get("blitz_best")),
            _v(row.get("profile_url")),
            _v(row.get("error")),
            row["username"],
        ],
    )


def _insert_new_players(conn: sqlite3.Connection, usernames: list[str]) -> list[str]:
    """Insert usernames not yet in player_information; return the newly inserted ones."""
    new = []
    for u in usernames:
        if not u:
            continue
        if conn.execute("SELECT 1 FROM player_information WHERE username = ?", (u,)).fetchone() is None:
            conn.execute("INSERT INTO player_information (username) VALUES (?)", (u,))
            new.append(u)
    conn.commit()
    return new


def enrich_players(conn: sqlite3.Connection, usernames: list[str], workers: int = 4) -> None:
    """Fetch chess.com profile data for each username and write it to player_information."""
    if not usernames:
        return
    _ensure_enrich_columns(conn)
    cache = PROJECT_ROOT / "data" / "api_cache"
    cache.mkdir(parents=True, exist_ok=True)
    print(f"  Enriching {len(usernames)} new player(s) via chess.com API…")
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_enrich_one, u, cache) for u in usernames]
        for fut in as_completed(futures):
            row = fut.result()
            with write_lock:
                _write_enriched_row(conn, row)
                conn.commit()
    print(f"  Enrichment done.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def slug_to_date(slug: str) -> str | None:
    m = re.search(r'-([a-z]+)-(\d{2})-(\d{4})-\d+$', slug)
    if not m:
        return None
    month_num = MONTH_MAP.get(m.group(1))
    if not month_num:
        return None
    return f'{m.group(3)}-{month_num:02d}-{int(m.group(2)):02d}'


def slug_to_title(slug: str) -> str:
    base = re.sub(r'-\d+$', '', slug)
    return '-'.join(w.capitalize() for w in base.split('-'))


def known_slugs() -> set:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('SELECT tournament_slug FROM titled_tuesday_tournaments').fetchall()
    conn.close()
    return {r[0] for r in rows}


def discover_slugs(max_pages: int = 5) -> list:
    """Scrape the completed Titled Tuesday listing pages for tournament slugs."""
    slugs = []
    for page in range(1, max_pages + 1):
        url  = LISTING if page == 1 else f'{LISTING}?page={page}'
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        found = TT_SLUG_RE.findall(resp.text)
        if not found:
            break
        slugs.extend(found)
        time.sleep(1.0)
    seen, ordered = set(), []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


# ── HTML parsing ───────────────────────────────────────────────────────────────

def parse_player_cell(cell) -> tuple:
    text = cell.get_text(strip=True)

    rank_m = re.match(r'^#(\d+)', text)
    rank   = int(rank_m.group(1)) if rank_m else None

    rating_m = re.search(r'\((\d+)\)$', text)
    rating   = int(rating_m.group(1)) if rating_m else None

    title = username = None
    for a in cell.find_all('a'):
        href = a.get('href', '')
        if '/members/' in href:
            title    = a.get_text(strip=True)
        elif '/member/' in href:
            username = a.get_text(strip=True)

    return rank, title, username, rating


def parse_round_cells(cells, round_indices: list) -> tuple[int, int, int]:
    wins = draws = byes = 0
    for i in round_indices:
        if i >= len(cells):
            break
        cell     = cells[i]
        text     = cell.get_text(strip=True)
        has_link = bool(cell.find('a'))
        if not text or text == '-':
            continue
        if not has_link:
            byes += 1
        elif text.startswith('1'):
            wins += 1
        elif text.startswith('.5'):
            draws += 1
    return wins, draws, byes


def fetch_standings(slug: str) -> list[dict]:
    players     = []
    page        = 1
    n_rounds    = None
    score_col   = tb1_col = None

    while True:
        url  = TOURN_URL.format(slug=slug, page=page)
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup  = BeautifulSoup(resp.text, 'html.parser')
        table = soup.find('table')
        if table is None:
            break

        if n_rounds is None:
            col_names     = [th.get_text(strip=True) for th in table.find_all('th')]
            score_col     = col_names.index('Pts.')
            tb1_col       = col_names.index('TB1')
            n_rounds      = score_col - 1
            round_indices = list(range(1, n_rounds + 1))

        rows = table.find_all('tr')[1:]
        if not rows:
            break

        for row in rows:
            cells = row.find_all('td')
            if not cells:
                continue
            rank, title, username, rating = parse_player_cell(cells[0])
            wins, draws, byes = parse_round_cells(cells, round_indices)
            score     = cells[score_col].get_text(strip=True) if score_col < len(cells) else None
            tie_break = cells[tb1_col].get_text(strip=True)   if tb1_col  < len(cells) else None
            if rank is None:
                rank = len(players) + 1
            players.append({
                'rank': rank, 'title': title, 'username': username, 'rating': rating,
                'score': score, 'tie_break': tie_break,
                'wins': wins, 'draws': draws, 'byes': byes,
            })

        print(f'  page {page}: {len(rows)} players fetched')
        if len(rows) < 25:
            break
        page += 1
        time.sleep(1.5)

    return players


# ── Main processing ────────────────────────────────────────────────────────────

def process(slug: str) -> None:
    print(f'Processing {slug} …')
    date    = slug_to_date(slug)
    title   = slug_to_title(slug)
    url     = f'https://www.chess.com/tournament/live/{slug}'
    session = 'early' if slug.startswith('early-') else 'late' if slug.startswith('late-') else None

    if date is None:
        print(f'  WARNING: could not parse date from slug — date will be blank')

    players = fetch_standings(slug)
    winner  = next((p['username'] for p in players if p['rank'] == 1), None)

    conn = sqlite3.connect(DB_PATH)

    # standings: delete old rows, insert new
    old_n = conn.execute(
        'SELECT COUNT(*) FROM titled_tuesday_standings WHERE tournament_slug = ?', (slug,)
    ).fetchone()[0]
    conn.execute('DELETE FROM titled_tuesday_standings WHERE tournament_slug = ?', (slug,))

    new_rows = pd.DataFrame([{
        'date':            f'{date} 00:00:00' if date else None,
        'tournament_slug': slug,
        'session':         session,
        'rank':            p['rank'],
        'username':        p['username'],
        'title':           p['title'],
        'country':         None,
        'rating':          p['rating'],
        'score':           p['score'],
        'tie_break':       p['tie_break'],
        'wins':            p['wins'],
        'draws':           p['draws'],
        'byes':            p['byes'],
    } for p in players])
    new_rows.to_sql('titled_tuesday_standings', conn, if_exists='append', index=False)
    print(f'  standings: replaced {old_n} rows -> {len(new_rows)} new')

    # tournaments: delete old row, insert new
    existing = pd.read_sql_query(
        'SELECT * FROM titled_tuesday_tournaments WHERE tournament_slug = ?', conn, params=(slug,)
    )
    conn.execute('DELETE FROM titled_tuesday_tournaments WHERE tournament_slug = ?', (slug,))

    new_tourn = {
        'date':            f'{date} 00:00:00' if date else None,
        'time_local':      existing.iloc[0]['time_local'] if not existing.empty else None,
        'title':           title,
        'session':         existing.iloc[0]['session'] if not existing.empty else session,
        'num_players':     len(players),
        'winner':          winner,
        'tournament_slug': slug,
        'url':             url,
    }
    pd.DataFrame([new_tourn]).to_sql('titled_tuesday_tournaments', conn, if_exists='append', index=False)
    print(f'  tournaments: num_players={len(players)}, winner={winner}')

    conn.commit()

    # enrich any usernames not yet in player_information
    all_usernames = [p['username'] for p in players if p['username']]
    new_usernames = _insert_new_players(conn, all_usernames)
    enrich_players(conn, new_usernames)

    conn.close()


def main() -> None:
    if len(sys.argv) > 1:
        process(sys.argv[1])
        return

    known = known_slugs()
    new   = [s for s in discover_slugs() if s not in known]
    if not new:
        print('No new tournaments found — DB is up to date.')
        return
    print(f'Found {len(new)} new tournament(s): {new}')
    for slug in new:
        process(slug)
        time.sleep(2.0)


if __name__ == '__main__':
    main()
