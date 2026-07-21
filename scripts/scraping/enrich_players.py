#!/usr/bin/env python3
"""Enrich player_information table with data from chess.com PubAPI (fast).

    https://api.chess.com/pub/player/{username}          -> name, title, country, status
    https://api.chess.com/pub/player/{username}/stats    -> blitz ratings + fide

Adds/updates these columns in player_information (data/titled_tuesday.db):
    player_name (COALESCE — preserves existing curated names),
    title, country, status, fide_rating,
    chess_com_blitz_rating (last), chess_com_blitz_best,
    profile_url, fetch_error

Speed: keep-alive sessions + a small thread pool. A global adaptive backoff
means any 429/403 pauses ALL workers, so the script self-regulates down to
serial if the server pushes back. Cache format is identical to the previous
serial version; a partial run's cache is reused as-is.

Resume: automatically skips rows where status IS NOT NULL or fetch_error IS NOT NULL.

Usage:
    python scripts/enrich_players.py                        # 4 workers
    python scripts/enrich_players.py --workers 1            # strict serial
    python scripts/enrich_players.py --offline              # cache only
    python scripts/enrich_players.py --limit 100            # first 100 unenriched (test)
"""
import argparse
import json
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import DB_PATH

API = "https://api.chess.com/pub/player"
UA  = {"User-Agent": "TT-attendance-research (personal project; contact via chess.com messages)"}

FIELDS = ["username", "name", "title", "country", "status",
          "fide", "blitz_last", "blitz_best", "profile_url", "error"]

# Columns that don't yet exist in player_information and need to be added
_NEW_COLS = {
    "status":               "TEXT",
    "chess_com_blitz_best": "INTEGER",
    "profile_url":          "TEXT",
    "fetch_error":          "TEXT",
}

_tls        = threading.local()   # one keep-alive session per thread
_pause_until = 0.0                # global backoff shared by all workers
_pause_lock  = threading.Lock()


def session() -> requests.Session:
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
        _tls.s.headers.update(UA)
    return _tls.s


def backoff(seconds: float):
    global _pause_until
    with _pause_lock:
        _pause_until = max(_pause_until, time.time() + seconds)


def wait_if_paused():
    while True:
        delta = _pause_until - time.time()
        if delta <= 0:
            return
        time.sleep(min(delta, 1.0))


def get_json(url: str, cache: Path, offline: bool, delay: float):
    """Fetch URL as JSON with caching, global adaptive backoff, retries."""
    key = cache / (re.sub(r"[^A-Za-z0-9._-]", "_", url.split("/pub/")[-1]) + ".json")
    if key.exists():
        return json.loads(key.read_text() or "null")
    if offline:
        return None
    for attempt in range(6):
        wait_if_paused()
        try:
            r = session().get(url, timeout=30)
            if r.status_code == 404:
                key.write_text("null")
                return None
            if r.status_code in (429, 403):
                wait = 15 * (attempt + 1)
                print(f"    HTTP {r.status_code}, global backoff {wait}s", file=sys.stderr)
                backoff(wait)
                continue
            r.raise_for_status()
            data = r.json()
            key.write_text(json.dumps(data))
            if delay:
                time.sleep(delay)
            return data
        except (requests.RequestException, json.JSONDecodeError):
            time.sleep(3 * (attempt + 1))
    return None


def enrich(username: str, cache: Path, offline: bool, delay: float) -> dict:
    time.sleep(random.uniform(0, delay + 1.0))   # stagger workers on startup and between tasks
    row = {f: "" for f in FIELDS}
    row["username"] = username
    prof = get_json(f"{API}/{username}", cache, offline, delay)
    if prof is None:
        row["error"] = "not_found_or_uncached"
        return row
    row["name"]        = prof.get("name", "")
    row["title"]       = prof.get("title", "")
    row["country"]     = (prof.get("country") or "").rsplit("/", 1)[-1]
    row["status"]      = prof.get("status", "")
    row["profile_url"] = prof.get("url", "")
    stats = get_json(f"{API}/{username}/stats", cache, offline, delay) or {}
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


def _ensure_columns(conn: sqlite3.Connection):
    """Add any missing enrichment columns to player_information."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(player_information)")}
    for col, dtype in _NEW_COLS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE player_information ADD COLUMN {col} {dtype}")
    conn.commit()


def _write_row(conn: sqlite3.Connection, row: dict):
    """UPDATE player_information with enriched data.

    Maps script field names to DB column names:
        name       -> player_name  (COALESCE: preserves existing curated names)
        fide       -> fide_rating
        blitz_last -> chess_com_blitz_rating
        blitz_best -> chess_com_blitz_best
        error      -> fetch_error

    Empty strings are stored as NULL.
    """
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache",   default=str(PROJECT_ROOT / "data" / "api_cache"),
                    help="directory for raw API response cache")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent workers (1 = strict serial)")
    ap.add_argument("--delay",   type=float, default=0.0,
                    help="extra per-request sleep in seconds")
    ap.add_argument("--offline", action="store_true", help="use cache only, no HTTP")
    ap.add_argument("--limit",   type=int, default=None,
                    help="only process first N unenriched usernames (for testing)")
    args = ap.parse_args()

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _ensure_columns(conn)

    q = ("SELECT username FROM player_information "
         "WHERE profile_url IS NULL AND fetch_error IS NULL "
         "ORDER BY username")
    if args.limit:
        q += f" LIMIT {args.limit}"
    todo  = [row[0] for row in conn.execute(q).fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM player_information").fetchone()[0]
    done_count = total - conn.execute(
        "SELECT COUNT(*) FROM player_information WHERE profile_url IS NULL AND fetch_error IS NULL"
    ).fetchone()[0]
    print(f"{len(todo)} unenriched usernames ({done_count}/{total} already done); "
          f"cache={cache}/ workers={args.workers}")

    t0         = time.time()
    write_lock = threading.Lock()
    n          = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(enrich, u, cache, args.offline, args.delay) for u in todo]
        for fut in as_completed(futures):
            row = fut.result()
            with write_lock:
                _write_row(conn, row)
                conn.commit()
                n += 1
                if n % 200 == 0 or n == len(todo):
                    rate = n / max(time.time() - t0, 1e-9)
                    eta  = (len(todo) - n) / max(rate, 1e-9)
                    print(f"  [{n}/{len(todo)}] {rate:.1f} users/s  ETA {eta/60:.0f} min")

    conn.close()
    print(f"done ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
