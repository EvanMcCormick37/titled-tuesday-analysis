"""
Update the Titled Tuesday standings/tournaments CSVs with new events.

Scrapes Chess.com tournament standings pages directly using BeautifulSoup,
paginating through all player pages:
    https://www.chess.com/tournament/live/{slug}?&players={page}

Recent Titled Tuesday URL IDs follow a predictable pattern:
    titled-tuesday-blitz-{month}-{dd}-{yyyy}-{numeric-id}
The numeric id is not predictable, so we discover new events by scraping
the completed-tournaments listing page.

Usage:
    pip install requests beautifulsoup4 pandas
    python update_titled_tuesday.py                  # process all new slugs
    python update_titled_tuesday.py <slug>           # process one specific slug

Existing rows with the same slug are replaced in both CSVs.
Be polite: sleeps between requests and sends a User-Agent header.
"""

import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent

STANDINGS_CSV    = BASE_DIR / 'data/titled_tuesday_standings.csv'
TOURNS_CSV       = BASE_DIR / 'data/titled_tuesday_tournaments.csv'

HEADERS  = {'User-Agent': 'titled-tuesday-research (contact: e.kidmccorm@gmail.com)'}
LISTING  = 'https://www.chess.com/tournament/live/titled-tuesdays'
TOURN_URL = 'https://www.chess.com/tournament/live/{slug}?&players={page}'

TT_SLUG_RE = re.compile(
    r'/tournament/live/((?:early-|late-)?titled-tuesday-blitz-[a-z]+-\d{2}-\d{4}-\d+)'
)

MONTH_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


# ── helpers ────────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df.loc[:, ~df.columns.str.startswith('Unnamed')]


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
    return set(load_csv(TOURNS_CSV)['slug'].dropna())


def discover_slugs(max_pages: int = 5) -> list:
    """Scrape the completed Titled Tuesday listing pages for tournament slugs."""
    slugs = []
    for page in range(1, max_pages + 1):
        url = LISTING if page == 1 else f'{LISTING}?page={page}'
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
    """Return (rank, title, username, rating) from the first td of a standings row."""
    text = cell.get_text(strip=True)

    rank_m = re.match(r'^#(\d+)', text)
    rank = int(rank_m.group(1)) if rank_m else None

    rating_m = re.search(r'\((\d+)\)$', text)
    rating = int(rating_m.group(1)) if rating_m else None

    title = username = None
    for a in cell.find_all('a'):
        href = a.get('href', '')
        if '/members/' in href:
            title = a.get_text(strip=True)
        elif '/member/' in href:
            username = a.get_text(strip=True)

    return rank, title, username, rating


def parse_round_cells(cells, round_indices: list) -> tuple[int, int, int]:
    """Count wins, draws, byes from the round td elements."""
    wins = draws = byes = 0
    for i in round_indices:
        if i >= len(cells):
            break
        cell = cells[i]
        text = cell.get_text(strip=True)
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
    """Paginate through all player pages and return a list of player dicts."""
    players = []
    page = 1
    n_rounds = None
    score_col = tb1_col = None

    while True:
        url = TOURN_URL.format(slug=slug, page=page)
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        table = soup.find('table')
        if table is None:
            break

        # Detect column layout from headers on first page
        if n_rounds is None:
            col_names = [th.get_text(strip=True) for th in table.find_all('th')]
            score_col = col_names.index('Pts.')
            tb1_col   = col_names.index('TB1')
            n_rounds  = score_col - 1  # columns between Player and Pts.
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

            players.append({
                'rank':      rank,
                'title':     title,
                'username':  username,
                'rating':    rating,
                'score':     score,
                'tie_break': tie_break,
                'wins':      wins,
                'draws':     draws,
                'byes':      byes,
            })

        print(f'  page {page}: {len(rows)} players fetched')
        if len(rows) < 25:
            break
        page += 1
        time.sleep(1.5)

    return players


# ── main processing ────────────────────────────────────────────────────────────

def process(slug: str) -> None:
    print(f'Processing {slug} …')
    date  = slug_to_date(slug)
    title = slug_to_title(slug)
    url   = f'https://www.chess.com/tournament/live/{slug}'

    if date is None:
        print(f'  WARNING: could not parse date from slug — date will be blank')

    session = 'early' if slug.startswith('early-') else 'late' if slug.startswith('late-') else ''

    players = fetch_standings(slug)
    winner  = next((p['username'] for p in players if p['rank'] == 1), None)

    # ── standings ──────────────────────────────────────────────────────────────
    standings = load_csv(STANDINGS_CSV)
    old_n = (standings['tournament_slug'] == slug).sum()
    standings = standings[standings['tournament_slug'] != slug]

    new_rows = pd.DataFrame([{
        'date':            date,
        'tournament_slug': slug,
        'session':         session or None,
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

    standings = pd.concat([standings, new_rows], ignore_index=True)
    standings.to_csv(STANDINGS_CSV, index=False)
    print(f'  standings: replaced {old_n} rows -> {len(new_rows)} new ({len(standings)} total)')

    # ── tournaments ────────────────────────────────────────────────────────────
    tourns   = load_csv(TOURNS_CSV)
    existing = tourns[tourns['slug'] == slug]

    new_tourn = {col: None for col in tourns.columns}
    new_tourn.update({
        'date':        date,
        'title':       title,
        'session':     session or None,
        'num_players': len(players),
        'winner':      winner,
        'slug':        slug,
        'url':         url,
    })
    if not existing.empty:
        for col in ('time_local', 'session'):
            if col in tourns.columns:
                new_tourn[col] = existing.iloc[0][col]

    tourns = tourns[tourns['slug'] != slug]
    tourns = pd.concat([tourns, pd.DataFrame([new_tourn])], ignore_index=True)
    tourns = tourns.sort_values('date').reset_index(drop=True)
    tourns.to_csv(TOURNS_CSV, index=False)
    print(f'  tournaments: num_players={len(players)}, winner={winner}')


def main() -> None:
    if len(sys.argv) > 1:
        process(sys.argv[1])
        return

    known = known_slugs()
    new   = [s for s in discover_slugs() if s not in known]
    if not new:
        print('No new tournaments found — CSVs are up to date.')
        return
    print(f'Found {len(new)} new tournament(s): {new}')
    for slug in new:
        process(slug)
        time.sleep(2.0)


if __name__ == '__main__':
    main()
