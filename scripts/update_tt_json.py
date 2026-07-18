"""
update_tt_json.py — Import chess.com tournament JSON into the SQLite DB.

Usage:
    python scripts/update_tt_json.py <slug>    # load data/new_tourneys/<slug>.json
    python scripts/update_tt_json.py           # load all *.json in data/new_tourneys/
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.config import DB_PATH, DATA_DIR

NEW_TOURNEYS_DIR = DATA_DIR / 'new_tourneys'

MONTH_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


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


def process(json_path: Path) -> None:
    slug = json_path.stem
    with open(json_path, encoding='utf-8') as f:
        raw = json.load(f)

    date   = slug_to_date(slug)
    title  = slug_to_title(slug)
    url    = f'https://www.chess.com/tournament/live/{slug}'
    winner = next((p['username'] for p in raw if p['rank'] == 1), None)

    if date is None:
        print(f'[{slug}] WARNING: could not parse date from slug — date will be blank')

    conn = sqlite3.connect(DB_PATH)

    # standings
    old_n = conn.execute(
        'SELECT COUNT(*) FROM titled_tuesday_standings WHERE tournament_slug = ?', (slug,)
    ).fetchone()[0]
    conn.execute('DELETE FROM titled_tuesday_standings WHERE tournament_slug = ?', (slug,))

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
    new_rows.to_sql('titled_tuesday_standings', conn, if_exists='append', index=False)
    print(f'[{slug}] standings: replaced {old_n} rows -> {len(new_rows)} new')

    # tournaments
    existing = pd.read_sql_query(
        'SELECT * FROM titled_tuesday_tournaments WHERE slug = ?', conn, params=(slug,)
    )
    conn.execute('DELETE FROM titled_tuesday_tournaments WHERE slug = ?', (slug,))

    new_tourn = {
        'date':        date,
        'time_local':  existing.iloc[0]['time_local'] if not existing.empty else None,
        'title':       title,
        'session':     existing.iloc[0]['session'] if not existing.empty else None,
        'num_players': len(raw),
        'winner':      winner,
        'slug':        slug,
        'url':         url,
    }
    pd.DataFrame([new_tourn]).to_sql('titled_tuesday_tournaments', conn, if_exists='append', index=False)
    print(f'[{slug}] tournaments: num_players={len(raw)}, winner={winner}')

    conn.commit()
    conn.close()


def main() -> None:
    if len(sys.argv) > 1:
        slug      = sys.argv[1]
        json_path = NEW_TOURNEYS_DIR / f'{slug}.json'
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
