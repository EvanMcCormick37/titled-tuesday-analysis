"""
update_tt_json.py — Import chess.com tournament JSON into the project CSVs.

Usage:
    python update_tt_json.py <slug>       # load data/<slug>.json
    python update_tt_json.py              # load all *.json in data/new_tourneys/

The slug is always the JSON filename stem (e.g. titled-tuesday-blitz-july-07-2026-6590341).
Existing rows with the same slug are replaced in both CSVs.
"""

import json
import re
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent

STANDINGS_CSV    = BASE_DIR / 'data/titled_tuesday_standings.csv'
TOURNS_CSV       = BASE_DIR / 'data/titled_tuesday_tournaments.csv'
NEW_TOURNEYS_DIR = BASE_DIR / 'data/new_tourneys'

MONTH_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def slug_to_date(slug: str) -> str | None:
    """Parse YYYY-MM-DD from a slug like 'titled-tuesday-blitz-july-07-2026-6590341'."""
    m = re.search(r'-([a-z]+)-(\d{2})-(\d{4})-\d+$', slug)
    if not m:
        return None
    month_num = MONTH_MAP.get(m.group(1))
    if not month_num:
        return None
    return f'{m.group(3)}-{month_num:02d}-{int(m.group(2)):02d}'


def slug_to_title(slug: str) -> str:
    """'titled-tuesday-blitz-july-07-2026-6590341' -> 'Titled-Tuesday-Blitz-July-07-2026'"""
    base = re.sub(r'-\d+$', '', slug)
    return '-'.join(w.capitalize() for w in base.split('-'))


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df.loc[:, ~df.columns.str.startswith('Unnamed')]


def process(json_path: Path) -> None:
    slug = json_path.stem
    with open(json_path, encoding='utf-8') as f:
        raw = json.load(f)

    date  = slug_to_date(slug)
    title = slug_to_title(slug)
    url   = f'https://www.chess.com/tournament/live/{slug}'
    winner = next((p['username'] for p in raw if p['rank'] == 1), None)

    if date is None:
        print(f'[{slug}] WARNING: could not parse date from slug — date will be blank')

    # ── standings ──────────────────────────────────────────────────────────
    standings = load_csv(STANDINGS_CSV)
    old_n = (standings['tournament_slug'] == slug).sum()
    standings = standings[standings['tournament_slug'] != slug]

    new_rows = pd.DataFrame([{
        'date':            date,
        'tournament_slug': slug,
        'session':         None,
        'rank':            p['rank'],
        'username':        p['username'],
        'title':           p.get('title'),
        'country':         p.get('country'),
        'rating':          p.get('rating'),
        'score':           p.get('score'),
        'tie_break':       p.get('tie_break'),
        'wins':            p.get('wins'),
        'draws':           p.get('draws'),
        'byes':            p.get('byes'),
    } for p in raw])

    standings = pd.concat([standings, new_rows], ignore_index=True)
    standings.to_csv(STANDINGS_CSV, index=False)
    print(f'[{slug}] standings: replaced {old_n} rows -> {len(new_rows)} new ({len(standings)} total)')

    # ── tournaments ────────────────────────────────────────────────────────
    tourns = load_csv(TOURNS_CSV)
    existing = tourns[tourns['slug'] == slug]

    # Build replacement row, preserving columns not derivable from JSON
    new_tourn = {col: None for col in tourns.columns}
    new_tourn.update({
        'date':        date,
        'title':       title,
        'num_players': len(raw),
        'winner':      winner,
        'slug':        slug,
        'url':         url,
    })
    # Preserve time_local and session from any existing row
    if not existing.empty:
        for col in ('time_local', 'session'):
            if col in tourns.columns:
                new_tourn[col] = existing.iloc[0][col]

    tourns = tourns[tourns['slug'] != slug]
    tourns = pd.concat([tourns, pd.DataFrame([new_tourn])], ignore_index=True)
    tourns = tourns.sort_values('date').reset_index(drop=True)
    tourns.to_csv(TOURNS_CSV, index=False)
    print(f'[{slug}] tournaments: num_players={len(raw)}, winner={winner}')


def main() -> None:
    if len(sys.argv) > 1:
        slug      = sys.argv[1]
        json_path = Path('data') / f'{slug}.json'
        if not json_path.exists():
            print(f'Error: {json_path} not found')
            sys.exit(1)
        process(json_path)
    else:
        if not NEW_TOURNEYS_DIR.exists():
            print(f'Error: {NEW_TOURNEYS_DIR} does not exist and no slug argument given')
            sys.exit(1)
        files = sorted(NEW_TOURNEYS_DIR.glob('*.json'))
        if not files:
            print(f'No JSON files found in {NEW_TOURNEYS_DIR}')
            sys.exit(0)
        for jp in files:
            process(jp)
        print(f'\nProcessed {len(files)} tournament(s).')


if __name__ == '__main__':
    main()
