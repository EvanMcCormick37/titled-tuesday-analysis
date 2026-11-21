#!/usr/bin/env python3
"""Probe chess.com/events with headless Chromium: does it load, and what data does it fetch?

Phase-0 spike for the attendance researcher's chess.com collector. Self-contained
(only needs `playwright`), so it can be copied into a bare Playwright container
on Railway and run there to test from a datacenter IP.

It loads the page the way a browser does and records every JSON response the
page makes, then looks for arrays of event-like objects in them. No stealth or
challenge-solving: if Cloudflare challenges the browser, that's the result.

Usage:
    pip install playwright && python -m playwright install chromium
    python scripts/chesscom_events_probe.py                    # summary only
    python scripts/chesscom_events_probe.py --out probe_out    # also save captured JSON
    python scripts/chesscom_events_probe.py --follow 3         # also open 3 event pages

Exit code: 0 events found, 2 challenged/blocked, 1 loaded but no events found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Response, sync_playwright

EVENTS_URL = 'https://www.chess.com/events'
EVENT_LINK_RE = re.compile(r'^https://www\.chess\.com/events/[a-z0-9-]+/?$')
DATE_KEYS = ('start', 'startDate', 'start_date', 'startTime', 'startsAt', 'date', 'begin', 'timestamp')
NAME_KEYS = ('name', 'title', 'eventName')


class Capture:
    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.json_bodies: list[tuple[str, object]] = []

    def on_response(self, resp: Response) -> None:
        ctype = resp.headers.get('content-type', '')
        entry = {'url': resp.url, 'status': resp.status, 'type': ctype.split(';')[0],
                 'cf_mitigated': resp.headers.get('cf-mitigated')}
        self.responses.append(entry)
        host = urlparse(resp.url).hostname or ''
        if 'json' in ctype and host.endswith('chess.com') and resp.status == 200:
            try:
                self.json_bodies.append((resp.url, resp.json()))
            except Exception:  # noqa: BLE001 — body unavailable (redirect, aborted)
                pass


def _event_arrays(obj: object, path: str = '$', found: list | None = None) -> list[tuple[str, list]]:
    """Find lists of dicts that look like events: have a name-ish and a date-ish key."""
    found = [] if found is None else found
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and len(dicts) >= len(obj) / 2:
            keys = set().union(*(d.keys() for d in dicts[:20]))
            if keys & set(NAME_KEYS) and (keys & set(DATE_KEYS) or any('date' in k.lower() or 'start' in k.lower() for k in keys)):
                found.append((path, dicts))
        for i, x in enumerate(obj[:50]):
            _event_arrays(x, f'{path}[{i}]', found)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _event_arrays(v, f'{path}.{k}', found)
    return found


def _sample(d: dict) -> str:
    name = next((d[k] for k in NAME_KEYS if k in d), '?')
    date_k = next((k for k in d if k in DATE_KEYS or 'date' in k.lower() or 'start' in k.lower()), None)
    extra = [k for k in ('players', 'rounds', 'schedule', 'url', 'slug', 'status', 'endDate', 'end') if k in d]
    return f'{str(name)[:60]!r}  {date_k}={str(d.get(date_k))[:30] if date_k else "-"}  has={extra}'


def load(page, url: str, cap: Capture, wait_s: float) -> dict:
    t = time.monotonic()
    try:
        main = page.goto(url, wait_until='domcontentloaded', timeout=45_000)
    except Exception as e:  # noqa: BLE001
        return {'url': url, 'error': str(e)[:200]}
    try:
        page.wait_for_load_state('networkidle', timeout=20_000)
    except Exception:  # noqa: BLE001 — long-polling pages never go idle; carry on
        pass
    page.wait_for_timeout(int(wait_s * 1000))
    title = page.title()
    return {
        'url': url, 'status': main.status if main else None, 'title': title,
        'cf_mitigated': main.headers.get('cf-mitigated') if main else None,
        'challenged': 'just a moment' in title.lower() or (main is not None and main.headers.get('cf-mitigated') == 'challenge'),
        'seconds': round(time.monotonic() - t, 1),
    }


def report_json(cap: Capture, start: int, out: Path | None, tag: str) -> int:
    n_events = 0
    bodies = cap.json_bodies[start:]
    print(f'  JSON responses from chess.com: {len(bodies)}')
    for i, (url, body) in enumerate(bodies):
        u = urlparse(url)
        shape = (f'list[{len(body)}]' if isinstance(body, list)
                 else f'keys={list(body)[:8]}' if isinstance(body, dict) else type(body).__name__)
        print(f'   - {u.path}{"?" + u.query[:60] if u.query else ""}  {shape}')
        for path, items in _event_arrays(body):
            n_events += len(items)
            print(f'       event-like array at {path}: {len(items)} items')
            for d in items[:5]:
                print(f'         {_sample(d)}')
        if out:
            safe = re.sub(r'[^a-zA-Z0-9]+', '_', u.path).strip('_')[:80] or 'root'
            (out / f'{tag}_{i:02d}_{safe}.json').write_text(json.dumps(body, indent=1)[:2_000_000], encoding='utf-8')
    return n_events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, help='directory to save captured JSON')
    ap.add_argument('--follow', type=int, default=2, help='event pages to open after the list (default 2)')
    ap.add_argument('--wait', type=float, default=4.0, help='extra seconds after load for late requests')
    args = ap.parse_args()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    cap = Capture()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on('response', cap.on_response)
        print(f'Browser: {browser.version}  UA: {page.evaluate("navigator.userAgent")}\n')

        print(f'== {EVENTS_URL}')
        info = load(page, EVENTS_URL, cap, args.wait)
        print(f'  {info}')
        if info.get('error') or info.get('challenged') or (info.get('status') or 0) >= 400:
            blocked = [r for r in cap.responses if r['status'] in (403, 429, 503) or r['cf_mitigated']]
            print(f'  blocked/challenged responses: {len(blocked)}')
            for r in blocked[:5]:
                print(f'   - {r["status"]} {r["cf_mitigated"]} {r["url"][:100]}')
            print('\nRESULT: challenged')
            browser.close()
            return 2

        n_events = report_json(cap, 0, args.out, 'list')
        links = sorted({h for h in page.eval_on_selector_all('a[href]', 'els => els.map(e => e.href)')
                        if EVENT_LINK_RE.match(h)})
        print(f'  DOM: {len(links)} event links')
        for h in links[:15]:
            print(f'   - {h}')

        for url in links[:args.follow]:
            print(f'\n== {url}')
            start = len(cap.json_bodies)
            info = load(page, url, cap, args.wait)
            print(f'  {info}')
            if not info.get('challenged'):
                report_json(cap, start, args.out, 'event')
                text = page.inner_text('body')[:20000].lower()
                print(f'  page text mentions: rounds={"round" in text} players={"players" in text or "participants" in text} '
                      f'schedule={"schedule" in text}')
        browser.close()

    found = n_events > 0 or bool(links)
    print(f'\nRESULT: {"ok" if found else "no_events"}  (event-like JSON items: {n_events}, DOM event links: {len(links)})')
    return 0 if found else 1


if __name__ == '__main__':
    sys.exit(main())
