#!/usr/bin/env python3
"""Download TWIC weekly PGN zips (twic{N}g.zip) for an issue or date range.

TWIC is free but donation-supported -- if you use this at scale, please
donate at https://theweekinchess.com/twic (Mark Crowther has curated this
weekly for 30+ years, and it's the backbone of this whole dataset).

Usage:
    python fetch_twic.py --start-date 2014-10-01 --end-date today --out ./twic_zips
    python fetch_twic.py --start-issue 1040 --end-issue 1653 --out ./twic_zips
"""
import argparse
import datetime as dt
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ZIPS = "https://theweekinchess.com/zips"
UA = {"User-Agent": "tt-attendance-research (personal project)"}

# Anchor for date<->issue estimation: TWIC 1625 published Mon 2025-12-29.
ANCHOR_ISSUE, ANCHOR_DATE = 1625, dt.date(2025, 12, 29)


def issue_for_date(d: dt.date) -> int:
    return ANCHOR_ISSUE + round((d - ANCHOR_DATE).days / 7)


def fetch(url: str, dest: Path) -> str:
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    tmp = dest.with_suffix(".part")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.rename(dest)
        return "ok"
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        return f"http {e.code}"
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return f"err {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-issue", type=int)
    ap.add_argument("--end-issue", type=int)
    ap.add_argument("--start-date", help="YYYY-MM-DD (alternative to --start-issue)")
    ap.add_argument("--end-date", default="today")
    ap.add_argument("--out", default="./twic_zips")
    ap.add_argument("--delay", type=float, default=1.0, help="be polite; it's one guy's server")
    args = ap.parse_args()

    if args.start_issue:
        lo, hi = args.start_issue, args.end_issue or issue_for_date(dt.date.today())
    else:
        start = dt.date.fromisoformat(args.start_date or "2014-10-01")
        end = dt.date.today() if args.end_date == "today" else dt.date.fromisoformat(args.end_date)
        # pad one issue on each side: estimation is approximate & events span issues
        lo, hi = issue_for_date(start) - 1, issue_for_date(end) + 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"issues {lo}..{hi} ({hi-lo+1} zips) -> {out}/")
    misses = 0
    for n in range(lo, hi + 1):
        dest = out / f"twic{n}g.zip"
        status = fetch(f"{ZIPS}/twic{n}g.zip", dest)
        if status not in ("ok", "skip"):
            misses += 1
            print(f"  twic{n}g.zip: {status}", file=sys.stderr)
            if misses > 5 and n > hi - 3:
                print("  (recent issues may not exist yet; stopping)", file=sys.stderr)
                break
        elif status == "ok":
            print(f"  twic{n}g.zip ok ({dest.stat().st_size/1e3:.0f} KB)")
            time.sleep(args.delay)
    print("done.")


if __name__ == "__main__":
    main()
