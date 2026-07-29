#!/usr/bin/env python3
"""
kalshi_titled_tuesday_historical_market_data.py

Dump Titled Tuesday event rosters from Kalshi into the project SQLite DB,
plus a price snapshot taken at 11 AM Eastern on the Thursday before each
tournament (5 days before close), for backtesting.

Kalshi's hierarchy is Series -> Event -> Market:
  Series  KXTITLEDTUESDAY            "Titled Tuesday chess competition winner"
  Event   KXTITLEDTUESDAY-26JUL14    one tournament
  Market  KXTITLEDTUESDAY-26JUL14-*  one player (the tradeable contract)

Three API quirks this script exists to handle:

1. LIVE/HISTORICAL SPLIT. Kalshi partitions data into live (~3 months) and
   historical tiers. Markets settled before the cutoff are absent from
   GET /markets and from GET /events?with_nested_markets=true; they live only
   under GET /historical/markets. Same for candlesticks. Events and series are
   never partitioned. For a Nov-2025-onward pull, most data is historical.

2. THE TWO CANDLESTICK ENDPOINTS RETURN DIFFERENT FIELD NAMES.
     live       /series/{s}/markets/{t}/candlesticks
                -> yes_bid.open_dollars, .close_dollars, volume_fp
     historical /historical/markets/{t}/candlesticks
                -> yes_bid.open,         .close,         volume
   Merging them naively yields silent nulls. Normalised below.

3. 10 SNAPSHOTS PER MARKET at 12-hour increments. The final snapshot is 12 h
   before close; the earliest is 120 h (5 days) before close. Targets:
   close-120h, close-108h, ..., close-12h (snapshot_num 1..10). A single
   candlestick request per market covers the full range; each snapshot picks
   the closest candle within +/-window_hours of its target.

No authentication required. Docs: https://docs.kalshi.com

Usage:
    # rosters only
    python kalshi_titled_tuesday_historical_market_data.py --since 2025-11-01

    # rosters + 10 price snapshots per market (close-120h to close-12h)
    python kalshi_titled_tuesday_historical_market_data.py --since 2025-11-01 --snapshot

    # snapshot with custom search tolerance and candle resolution
    python kalshi_titled_tuesday_historical_market_data.py --since 2025-11-01 --snapshot \\
        --window-hours 4 --interval 60

    # use a different DB path
    python kalshi_titled_tuesday_historical_market_data.py --since 2025-11-01 --snapshot \\
        --db /path/to/other.db
"""

import argparse
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo

# ── Project paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import DB_PATH

ET = ZoneInfo("America/New_York")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = "titled-tuesday-archiver/2.0"

CANDIDATE_CATEGORIES = [
    "Sports", "Esports", "Entertainment", "Science and Technology", "World",
]

MATCH = re.compile(r"titled\s*tuesday", re.I)
DATECODE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def api_get(path, params=None, retries=6, sleep=0.15, quiet404=True):
    """GET with exponential backoff. Kalshi 429s carry no Retry-After and no
    X-RateLimit-* headers, so blind backoff with jitter is the only option."""
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + parse.urlencode(clean)

    delay = 1.0
    for attempt in range(retries):
        req = request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=45) as resp:
                time.sleep(sleep)
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code in (400, 404) and quiet404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay + random.random() * 0.4)
                delay = min(delay * 2, 30.0)
                continue
            body = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code} on {url}\n{body}") from None
        except (error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay + random.random() * 0.4)
                delay = min(delay * 2, 30.0)
                continue
            raise
    return None


def paginate(path, params, key, sleep=0.15):
    out, cursor, seen = [], None, set()
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = api_get(path, p, sleep=sleep)
        if not data:
            break
        batch = data.get(key) or []
        out.extend(batch)
        cursor = (data.get("cursor") or "").strip()
        if not cursor or not batch or cursor in seen:
            break
        seen.add(cursor)
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def fpv(node, base):
    """Read a candlestick field under either naming convention.
    live: '<base>_dollars' / 'volume_fp'   historical: '<base>' / 'volume'"""
    if not isinstance(node, dict):
        return ""
    return pick(node, f"{base}_dollars", base)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def tournament_date(event_ticker):
    m = DATECODE.search(event_ticker or "")
    if not m:
        return ""
    yy, mon, dd = m.groups()
    if mon not in MONTHS:
        return ""
    return f"20{yy}-{MONTHS[mon]:02d}-{int(dd):02d}"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def snapshot_targets(close_time_str):
    """Return list of (snapshot_num, target_dt) for 10 evenly-spaced snapshots.

    snapshot_num 1  → close - 120 h  (earliest, ~5 days out)
    snapshot_num 10 → close - 12 h   (latest, half-day before close)
    Step: 12 h between consecutive snapshots.
    Returns [] if close_time_str can't be parsed.
    """
    ct = parse_ts(close_time_str)
    if not ct:
        return []
    return [(n, ct - timedelta(hours=(11 - n) * 12)) for n in range(1, 11)]


# --------------------------------------------------------------------------
# Discovery / fetching
# --------------------------------------------------------------------------

def discover_series(sleep=0.15):
    found = {}
    for cat in CANDIDATE_CATEGORIES:
        data = api_get("/series", {"category": cat, "include_volume": "true"},
                       sleep=sleep)
        if not data:
            continue
        for s in data.get("series") or []:
            if MATCH.search(s.get("title", "")) or \
               "TITLEDTUESDAY" in s.get("ticker", "").upper():
                found[s["ticker"]] = s
    return found


def fetch_events(series_ticker, sleep=0.15):
    """Never partitioned by the cutoff, so this is the complete list.
    with_nested_markets is deliberately unused -- it drops old markets."""
    return paginate("/events", {"series_ticker": series_ticker, "limit": 200},
                    "events", sleep=sleep)


def fetch_markets(series_ticker, sleep=0.15):
    live = paginate("/markets", {"series_ticker": series_ticker, "limit": 1000},
                    "markets", sleep=sleep)
    hist = paginate("/historical/markets",
                    {"series_ticker": series_ticker, "limit": 1000},
                    "markets", sleep=sleep)
    for m in hist:
        m["_tier"] = "historical"
    for m in live:
        m["_tier"] = "live"
    merged = {m["ticker"]: m for m in hist}
    merged.update({m["ticker"]: m for m in live})
    return list(merged.values()), len(live), len(hist)


def fetch_candles(series, ticker, start_ts, end_ts, interval, tier, sleep):
    """Try the tier we think the market is in, then fall back to the other."""
    live_path = f"/series/{parse.quote(series)}/markets/{parse.quote(ticker)}/candlesticks"
    hist_path = f"/historical/markets/{parse.quote(ticker)}/candlesticks"
    order = [("historical", hist_path), ("live", live_path)] \
        if tier == "historical" else [("live", live_path), ("historical", hist_path)]

    params = {"start_ts": int(start_ts), "end_ts": int(end_ts),
              "period_interval": interval}
    for name, path in order:
        data = api_get(path, params, sleep=sleep)
        candles = (data or {}).get("candlesticks") or []
        if candles:
            return candles, name
    return [], ""


# --------------------------------------------------------------------------
# Row shaping
# --------------------------------------------------------------------------

def roster_row(series, ev, m):
    ot, ct = parse_ts(m.get("open_time")), parse_ts(m.get("close_time"))
    lead = round((ct - ot).total_seconds() / 3600, 2) if ot and ct else None
    return {
        "series_ticker": series,
        "event_ticker": ev.get("event_ticker", ""),
        "event_title": ev.get("title", ""),
        "tournament_date": tournament_date(ev.get("event_ticker", "")),
        "market_ticker": m.get("ticker", ""),
        "player": pick(m, "yes_sub_title", "subtitle", "title"),
        "status": m.get("status", ""),
        "result": m.get("result", ""),
        "settlement_value": num(pick(m, "settlement_value_dollars", "settlement_value")),
        "open_time": m.get("open_time", ""),
        "close_time": m.get("close_time", ""),
        "lead_hours": lead,
        "volume": num(pick(m, "volume_fp", "volume")),
        "open_interest": num(pick(m, "open_interest_fp", "open_interest")),
        "tier": m.get("_tier", ""),
    }


def snapshot_row(r, candles, tier_used, target, snapshot_num, window_hours=6.0):
    """Pick the candle closest to target (within window_hours) and normalise fields."""
    max_err_s = window_hours * 3600
    best, best_err = None, None
    for c in candles:
        ets = c.get("end_period_ts")
        if not ets:
            continue
        err = abs(ets - target.timestamp())
        if err <= max_err_s and (best_err is None or err < best_err):
            best, best_err = c, err

    out = {k: r.get(k) for k in
           ("series_ticker", "event_ticker", "tournament_date", "market_ticker",
            "player", "open_time", "close_time", "lead_hours", "result",
            "settlement_value")}
    out.update({"snapshot_num": snapshot_num,
                "target_time": iso(target), "candles_in_window": len(candles),
                "tier_used": tier_used, "candle_time": None,
                "actual_hours_after_open": None, "offset_error_h": None,
                "yes_bid_close": None, "yes_ask_close": None, "mid": None,
                "spread": None, "last_trade_previous": None,
                "cum_volume": None, "open_interest": None})
    if not best:
        return out

    ctime = datetime.fromtimestamp(best["end_period_ts"], tz=timezone.utc)
    ot = parse_ts(r.get("open_time"))
    bid = num(fpv(best.get("yes_bid"), "close"))
    ask = num(fpv(best.get("yes_ask"), "close"))

    out["candle_time"] = iso(ctime)
    out["offset_error_h"] = round(best_err / 3600, 2)
    if ot:
        out["actual_hours_after_open"] = round(
            (ctime - ot).total_seconds() / 3600, 2)
    out["yes_bid_close"] = bid
    out["yes_ask_close"] = ask
    if bid is not None and ask is not None:
        out["mid"] = round((bid + ask) / 2, 4)
        out["spread"] = round(ask - bid, 4)
    out["last_trade_previous"] = num(fpv(best.get("price"), "previous"))
    out["cum_volume"] = num(pick(best, "volume_fp", "volume"))
    out["open_interest"] = num(pick(best, "open_interest_fp", "open_interest"))
    return out


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

def create_tables(conn):
    # Drop old single-snapshot table (market_ticker-only PK) if it exists
    existing = {row[1] for row in
                conn.execute("PRAGMA table_info(kalshi_market_snapshots)")}
    if existing and "snapshot_num" not in existing:
        conn.execute("DROP TABLE kalshi_market_snapshots")
        conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            market_ticker     TEXT PRIMARY KEY,
            series_ticker     TEXT,
            event_ticker      TEXT,
            event_title       TEXT,
            tournament_date   TEXT,
            player            TEXT,
            status            TEXT,
            result            TEXT,
            settlement_value  REAL,
            open_time         TEXT,
            close_time        TEXT,
            lead_hours        REAL,
            volume            REAL,
            open_interest     REAL,
            tier              TEXT
        );
        CREATE TABLE IF NOT EXISTS kalshi_market_snapshots (
            market_ticker          TEXT,
            snapshot_num           INTEGER,
            series_ticker          TEXT,
            event_ticker           TEXT,
            tournament_date        TEXT,
            player                 TEXT,
            open_time              TEXT,
            close_time             TEXT,
            lead_hours             REAL,
            target_time            TEXT,
            candle_time            TEXT,
            actual_hours_after_open REAL,
            offset_error_h         REAL,
            yes_bid_close          REAL,
            yes_ask_close          REAL,
            mid                    REAL,
            spread                 REAL,
            last_trade_previous    REAL,
            cum_volume             REAL,
            open_interest          REAL,
            result                 TEXT,
            settlement_value       REAL,
            candles_in_window      INTEGER,
            tier_used              TEXT,
            PRIMARY KEY (market_ticker, snapshot_num)
        );
    """)
    conn.commit()


def upsert_roster(conn, rows):
    conn.executemany("""
        INSERT OR REPLACE INTO kalshi_markets
            (market_ticker, series_ticker, event_ticker, event_title,
             tournament_date, player, status, result, settlement_value,
             open_time, close_time, lead_hours, volume, open_interest, tier)
        VALUES
            (:market_ticker, :series_ticker, :event_ticker, :event_title,
             :tournament_date, :player, :status, :result, :settlement_value,
             :open_time, :close_time, :lead_hours, :volume, :open_interest, :tier)
    """, rows)
    conn.commit()


def upsert_snapshot(conn, row):
    conn.execute("""
        INSERT OR REPLACE INTO kalshi_market_snapshots
            (market_ticker, snapshot_num, series_ticker, event_ticker,
             tournament_date, player, open_time, close_time, lead_hours,
             target_time, candle_time, actual_hours_after_open, offset_error_h,
             yes_bid_close, yes_ask_close, mid, spread,
             last_trade_previous, cum_volume, open_interest,
             result, settlement_value, candles_in_window, tier_used)
        VALUES
            (:market_ticker, :snapshot_num, :series_ticker, :event_ticker,
             :tournament_date, :player, :open_time, :close_time, :lead_hours,
             :target_time, :candle_time, :actual_hours_after_open, :offset_error_h,
             :yes_bid_close, :yes_ask_close, :mid, :spread,
             :last_trade_previous, :cum_volume, :open_interest,
             :result, :settlement_value, :candles_in_window, :tier_used)
    """, row)
    conn.commit()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", nargs="*", default=None)
    ap.add_argument("--db", default=str(DB_PATH),
                    help="Path to SQLite database (default: project DB_PATH).")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--since", default="2025-11-01",
                    help="Earliest tournament date, YYYY-MM-DD.")
    ap.add_argument("--until", default=None)
    ap.add_argument("--snapshot", action="store_true",
                    help="Pull 10 candlestick snapshots per market at 12-hour "
                         "increments from close-120h to close-12h.")
    ap.add_argument("--window-hours", type=float, default=6.0,
                    help="Per-snapshot candle search radius in hours (default 6).")
    ap.add_argument("--interval", type=int, default=60, choices=[1, 60, 1440],
                    help="Candle length in minutes (default 60).")
    args = ap.parse_args()

    cut = api_get("/historical/cutoff", sleep=args.sleep)
    if cut:
        print(f"Live/historical cutoff: {json.dumps(cut)}\n")

    if args.series:
        series_list = {t: {"ticker": t} for t in args.series}
    else:
        print("Discovering Titled Tuesday series...")
        series_list = discover_series(args.sleep)
        if not series_list:
            print("No matching series found. Pass --series explicitly.",
                  file=sys.stderr)
            return 1
        for t, s in sorted(series_list.items()):
            print(f"  {t:<30} {s.get('title', '')}")
        print()

    conn = sqlite3.connect(args.db)
    create_tables(conn)

    rows, raw = [], []
    for series in sorted(series_list):
        print(f"[{series}]")
        events = fetch_events(series, args.sleep)
        markets, n_live, n_hist = fetch_markets(series, args.sleep)
        print(f"  {len(events)} events, {len(markets)} markets "
              f"({n_live} live / {n_hist} historical)")

        by_event = {}
        for m in markets:
            by_event.setdefault(m.get("event_ticker", ""), []).append(m)

        # /events only returns live-tier events; historical markets carry their
        # event_ticker but the corresponding event records aren't served by the
        # endpoint.  Synthesise stub entries so historical markets are included.
        live_event_tickers = {ev.get("event_ticker", "") for ev in events}
        for et in by_event:
            if et and et not in live_event_tickers:
                events.append({"event_ticker": et, "title": ""})

        kept = 0
        for ev in events:
            et = ev.get("event_ticker", "")
            tdate = tournament_date(et)
            if not tdate or tdate < args.since:
                continue
            if args.until and tdate > args.until:
                continue
            roster = sorted(by_event.get(et, []), key=lambda m: m["ticker"])
            for m in roster:
                rows.append(roster_row(series, ev, m))
            raw.append({**ev, "markets": roster})
            kept += 1
        print(f"  {kept} events in window (>= {args.since})")

    rows.sort(key=lambda r: (r["tournament_date"], r["series_ticker"],
                             r["market_ticker"]))

    upsert_roster(conn, rows)

    dates = sorted({r["tournament_date"] for r in rows if r["tournament_date"]})
    print(f"\n{len(rows)} contracts / {len(raw)} events written to kalshi_markets")
    if dates:
        print(f"Date range: {dates[0]} -> {dates[-1]}  ({len(dates)} tournaments)")

    leads = sorted(x for x in (r["lead_hours"] for r in rows) if x)
    if leads:
        mid = leads[len(leads) // 2]
        print(f"Market open->close hours: min {leads[0]:.1f} / "
              f"median {mid:.1f} / max {leads[-1]:.1f}")

    if not args.snapshot:
        conn.close()
        return 0

    # ---------------- snapshot pass ----------------
    n_targets = 10
    print(f"\nSnapshotting {len(rows)} markets x {n_targets} snapshots "
          f"(close-120h to close-12h, +/-{args.window_hours}h, "
          f"{args.interval}m candles)...")

    hits = 0
    for i, r in enumerate(rows, 1):
        targets = snapshot_targets(r.get("close_time"))
        if not targets:
            continue

        ct = parse_ts(r["close_time"])
        lo = ct - timedelta(hours=120 + args.window_hours)
        hi = ct - timedelta(hours=12 - args.window_hours)
        if hi > ct:
            hi = ct

        candles, tier_used = fetch_candles(
            r["series_ticker"], r["market_ticker"],
            lo.timestamp(), hi.timestamp(), args.interval,
            r.get("tier", ""), args.sleep)

        for snap_num, target in targets:
            row = snapshot_row(r, candles, tier_used, target,
                               snap_num, args.window_hours)
            if row["yes_bid_close"] is not None or row["yes_ask_close"] is not None:
                hits += 1
            upsert_snapshot(conn, row)

        if i % 50 == 0:
            print(f"  {i}/{len(rows)} markets  ({hits} snapshots with quotes)")

    conn.close()
    print(f"\n{hits}/{len(rows) * n_targets} snapshots had a two-sided quote")
    print(f"Wrote kalshi_markets and kalshi_market_snapshots to {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
