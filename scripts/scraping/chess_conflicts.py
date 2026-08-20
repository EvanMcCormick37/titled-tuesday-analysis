#!/usr/bin/env python3
"""
chess_conflicts.py — check which active Titled Tuesday players have an OTB
tournament round on the upcoming Tuesday, using chess-results.com's player search.

Usage:
    python chess_conflicts.py                        # next Tuesday, players from DB
    python chess_conflicts.py --date 2026-08-12      # specific date
    python chess_conflicts.py --limit 10             # dry-run first 10 players
    python chess_conflicts.py --dump-fields          # debug form field discovery
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH, DATA_DIR

BASE = "https://s3.chess-results.com"
SEARCH_URL = f"{BASE}/spielersuche.aspx?lan=1"
TNR_URL = f"{BASE}/tnr{{tnr}}.aspx?lan=1"

# Substring hints used to identify form controls (lowercase, first match wins).
FIELD_HINTS = {
    "last_name":  ["nachname", "lastname", "surname"],
    "first_name": ["vorname", "firstname"],
    "fide_id":    ["fideid", "fide_id", "fide"],
    "date_from":  ["von", "datefrom", "from"],
    "date_to":    ["bis", "dateto", "to"],
    "search_btn": ["suchen", "search", "cb_such"],
}

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
ROUND_RE = re.compile(r"\b(?:round|runde|rd\.?)\s*(\d+)\b", re.I)


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def norm(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_latin(s: str) -> bool:
    return all(ord(c) < 0x0250 or c.isspace() for c in s)


def name_tokens(s: str) -> set[str]:
    return {t for t in norm(s).split() if len(t) > 1}


def name_similarity(a: str, b: str) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb) / min(len(ta), len(tb))
    fuzzy = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(overlap, fuzzy)


def surname_candidates(full_name: str) -> list[str]:
    toks = [t for t in full_name.split() if len(t) > 1]
    if not toks:
        return []
    out = [toks[-1]]
    if len(toks) > 1 and toks[0].lower() != toks[-1].lower():
        out.append(toks[0])
    return out


def parse_dates(text: str) -> list[dt.date]:
    out = []
    for d, m, y in DATE_RE.findall(text):
        try:
            out.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

class Cache:
    def __init__(self, path: str, enabled: bool = True):
        self.path, self.enabled = path, enabled
        if enabled:
            os.makedirs(path, exist_ok=True)

    def _f(self, key: str) -> str:
        return os.path.join(self.path, hashlib.sha256(key.encode()).hexdigest()[:32] + ".html")

    def get(self, key: str):
        if not self.enabled:
            return None
        f = self._f(key)
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                return fh.read()
        return None

    def put(self, key: str, val: str):
        if not self.enabled:
            return
        with open(self._f(key), "w", encoding="utf-8") as fh:
            fh.write(val)


# --------------------------------------------------------------------------
# chess-results client
# --------------------------------------------------------------------------

class ChessResults:
    def __init__(self, delay: float = 2.0, cache: Cache | None = None, verbose: bool = False):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.delay = delay
        self.cache = cache or Cache("", enabled=False)
        self.verbose = verbose
        self.fields: dict[str, str] = {}
        self._last_request = 0.0

    def _throttle(self):
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _request(self, method: str, url: str, cache_key: str | None = None, **kw) -> str:
        if cache_key:
            hit = self.cache.get(cache_key)
            if hit is not None:
                if self.verbose:
                    print(f"  [cache] {cache_key[:70]}", file=sys.stderr)
                return hit

        last_err = None
        for attempt in range(4):
            self._throttle()
            try:
                r = self.s.request(method, url, timeout=45, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                if cache_key:
                    self.cache.put(cache_key, r.text)
                return r.text
            except Exception as e:
                last_err = e
                backoff = 4 * (2 ** attempt)
                print(f"  ! {e} — retry in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
        raise RuntimeError(f"failed after retries: {last_err}")

    @staticmethod
    def _form_state(soup: BeautifulSoup) -> dict[str, str]:
        state = {}
        for inp in soup.select("input[type=hidden]"):
            if inp.get("name"):
                state[inp["name"]] = inp.get("value", "")
        return state

    @staticmethod
    def _all_controls(soup: BeautifulSoup) -> list[str]:
        names = []
        for el in soup.select("input, select, textarea"):
            n = el.get("name")
            if n and not n.startswith("__"):
                names.append(n)
        return names

    def discover_fields(self, soup: BeautifulSoup) -> dict[str, str]:
        controls = self._all_controls(soup)
        mapping: dict[str, str] = {}
        for semantic, hints in FIELD_HINTS.items():
            for hint in hints:
                match = next((c for c in controls
                              if hint in c.lower().split("$")[-1]
                              and c not in mapping.values()), None)
                if match:
                    mapping[semantic] = match
                    break
        return mapping

    def load_search_page(self) -> tuple[BeautifulSoup, dict[str, str]]:
        html = self._request("GET", SEARCH_URL, cache_key=None)
        soup = BeautifulSoup(html, "lxml")
        if not self.fields:
            self.fields = self.discover_fields(soup)
        return soup, self._form_state(soup)

    def search_surname(self, surname: str, date_from: dt.date, date_to: dt.date) -> list[dict]:
        cache_key = f"search|{surname}|{date_from}|{date_to}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return self._parse_results(BeautifulSoup(cached, "lxml"))

        soup, payload = self.load_search_page()
        f = self.fields
        missing = [k for k in ("last_name", "search_btn") if k not in f]
        if missing:
            raise RuntimeError(
                f"Could not locate form fields {missing}. "
                f"Run --dump-fields and update FIELD_HINTS."
            )

        payload[f["last_name"]] = surname
        if "date_from" in f:
            payload[f["date_from"]] = date_from.strftime("%Y-%m-%d")
        if "date_to" in f:
            payload[f["date_to"]] = date_to.strftime("%Y-%m-%d")
        payload[f["search_btn"]] = "Search"

        html = self._request("POST", SEARCH_URL, data=payload,
                             headers={"Referer": SEARCH_URL})
        self.cache.put(cache_key, html)
        return self._parse_results(BeautifulSoup(html, "lxml"))

    @staticmethod
    def _parse_results(soup: BeautifulSoup) -> list[dict]:
        rows = []
        for tr in soup.find_all("tr"):
            link = tr.find("a", href=re.compile(r"tnr(\d+)\.aspx", re.I))
            if not link:
                continue
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            tnr = re.search(r"tnr(\d+)\.aspx", link["href"], re.I).group(1)

            joined = " | ".join(cells)
            fide = next((c for c in cells if re.fullmatch(r"\d{6,10}", c)), "")
            dates = parse_dates(joined)

            tname = link.get_text(" ", strip=True)
            pname = ""
            for c in cells:
                if c == tname or not c:
                    continue
                if re.search(r"[A-Za-zÀ-ɏ]{2,}", c) and not re.fullmatch(r"[\d.,\s]+", c):
                    pname = c
                    break

            rows.append({
                "tnr": tnr,
                "tournament": tname,
                "player": pname,
                "fide_id": fide,
                "end_date": max(dates) if dates else None,
                "raw": joined,
                "url": TNR_URL.format(tnr=tnr),
            })
        return rows

    def tournament_schedule(self, tnr: str) -> dict:
        html = self._request("GET", TNR_URL.format(tnr=tnr), cache_key=f"tnr|{tnr}")
        return self._parse_schedule(html)

    @staticmethod
    def _parse_schedule(html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)

        round_dates: dict[int, dt.date] = {}
        for tr in soup.find_all("tr"):
            line = tr.get_text(" ", strip=True)
            rm, dates = ROUND_RE.search(line), parse_dates(line)
            if rm and dates:
                round_dates[int(rm.group(1))] = dates[0]
        if not round_dates:
            for line in text.split("\n"):
                rm, dates = ROUND_RE.search(line), parse_dates(line)
                if rm and dates:
                    round_dates.setdefault(int(rm.group(1)), dates[0])

        all_dates = parse_dates(text)
        span = (min(all_dates), max(all_dates)) if all_dates else (None, None)

        return {"round_dates": round_dates, "span": span}


# --------------------------------------------------------------------------
# verdict logic
# --------------------------------------------------------------------------

def verdict(sched: dict, target: dt.date) -> tuple[str, str]:
    rd, (start, end) = sched["round_dates"], sched["span"]
    if rd:
        hits = sorted(n for n, d in rd.items() if d == target)
        if hits:
            return "YES", "round " + ", ".join(map(str, hits)) + f" on {target:%d.%m.%Y}"
        if start and end and start <= target <= end:
            return "NO", f"schedule found ({len(rd)} rounds), none on {target:%d.%m.%Y} — likely rest day"
        return "NO", "target date outside tournament schedule"
    if start and end and start <= target <= end:
        return "POSSIBLE", f"tournament runs {start:%d.%m.%Y}–{end:%d.%m.%Y}; no per-round dates published"
    return "NO", "no schedule overlap detected"


# --------------------------------------------------------------------------
# player loading from DB
# --------------------------------------------------------------------------

def next_tuesday() -> dt.date:
    today = dt.date.today()
    days_ahead = (1 - today.weekday()) % 7
    return today + dt.timedelta(days=days_ahead or 7)


def load_players_from_db() -> list[dict]:
    """Load players with p_top10 > 1% from latest_model_predictions_raw joined with player_information."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT DISTINCT pi.player_name,
               lmp.p_participate * lmp.P_top10_given_play AS p_top10
        FROM latest_model_predictions_raw lmp
        JOIN player_information pi ON lmp.username = pi.username
        WHERE pi.player_name IS NOT NULL
          AND pi.player_name != ''
          AND pi.player_name GLOB '*[A-Za-z]*'
          AND lmp.p_participate * lmp.P_top10_given_play > 0.01
        ORDER BY p_top10 DESC
    """).fetchall()
    conn.close()
    if not rows:
        raise RuntimeError("No players found in latest_model_predictions_raw. Run make_predictions.py first.")
    return [{"name": row[0], "fide_id": ""} for row in rows]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="target Tuesday date (YYYY-MM-DD); defaults to next Tuesday")
    ap.add_argument("--window-days", type=int, default=10,
                    help="how far past the target to look for tournament END dates")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--threshold", type=float, default=0.80, help="name match threshold 0–1")
    ap.add_argument("--limit", type=int, help="only process the first N players")
    ap.add_argument("--cache-dir", default=str(DATA_DIR / "cr_cache"))
    ap.add_argument("--dump-fields", action="store_true",
                    help="print discovered form controls and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    cache = Cache(a.cache_dir, enabled=False)
    cr = ChessResults(delay=a.delay, cache=cache, verbose=a.verbose)

    if a.dump_fields:
        soup, _ = cr.load_search_page()
        print("All form controls on spielersuche.aspx:\n")
        for n in cr._all_controls(soup):
            print("   ", n)
        print("\nAuto-detected mapping:\n")
        for k, v in cr.discover_fields(soup).items():
            print(f"    {k:12s} -> {v}")
        unmapped = set(FIELD_HINTS) - set(cr.discover_fields(soup))
        if unmapped:
            print(f"\n  NOT MAPPED: {sorted(unmapped)}  <- fix FIELD_HINTS")
        return 0

    target = dt.date.fromisoformat(a.date) if a.date else next_tuesday()
    date_from, date_to = target, target + dt.timedelta(days=a.window_days)

    players = load_players_from_db()
    if a.limit:
        players = players[:a.limit]

    non_latin = [p["name"] for p in players if not is_latin(p["name"])]
    if non_latin:
        print(f"NOTE: {len(non_latin)} name(s) are non-Latin script — may need manual checking:",
              file=sys.stderr)
        for n in non_latin:
            print(f"   - {n}", file=sys.stderr)

    by_surname: dict[str, list[dict]] = defaultdict(list)
    for p in players:
        for sn in surname_candidates(p["name"]):
            by_surname[sn].append(p)

    print(f"Checking {len(players)} players for rounds on {target:%Y-%m-%d} "
          f"({len(by_surname)} surname queries)", file=sys.stderr)

    # Phase 1: search by surname
    candidates: list[dict] = []
    for i, (surname, plist) in enumerate(sorted(by_surname.items()), 1):
        print(f"[{i}/{len(by_surname)}] {surname!r}", file=sys.stderr)
        try:
            hits = cr.search_surname(surname, date_from, date_to)
        except Exception as e:
            print(f"  !! {surname}: {e}", file=sys.stderr)
            continue
        for h in hits:
            for p in plist:
                score = name_similarity(p["name"], h["player"])
                if score >= a.threshold:
                    candidates.append({**h, "input_name": p["name"], "score": round(score, 3)})

    seen, uniq = set(), []
    for c in candidates:
        k = (c["input_name"], c["tnr"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)

    uniq.sort(key=lambda c: c["input_name"])

    print(f"\n{'='*70}")
    print(f"Titled Tuesday candidate conflicts — {target:%Y-%m-%d}")
    print(f"{'='*70}")
    print(f"\n{len(uniq)} player/tournament pair(s) to review manually:\n")

    for c in uniq:
        print(f"  {c['input_name']:<32}  (match: {c['score']:.2f})  {c['player']}")
        print(f"    {c['tournament']}")
        print(f"    ends: {c['end_date'] or 'unknown':<12}  {c['url']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
