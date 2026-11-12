"""Turn chess.com's public tournament API responses into Bullet Brawl standings rows.

The round payload includes every game, which is used only to derive per-player
totals. Individual games are not stored.

Usernames keep chess.com's capitalization, as Titled Tuesday's do, so one player
has one username across markets and in player_information. The round's player
list is lowercase, so the case comes from the player's games, and `write_event`
then adopts whatever case is already on record for that account.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

API = 'https://api.chess.com/pub/tournament/{slug}'
HEADERS = {'User-Agent': 'tournament-standings-research'}
PAGE_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; tournament-standings-research)'}
DRAW_RESULTS = {'agreed', 'repetition', 'stalemate', 'insufficient', '50move', 'timevsinsufficient'}
_WIN_POINTS = (2, 3)      # first and second consecutive win; every later win is 4
_STREAK_POINTS = 4

ET = ZoneInfo('America/New_York')
# The official event only: a community "...-bullet-brawl-..." arena doesn't start the slug.
EVENT_SLUG = re.compile(r'/tournament/(?:live/(?:arena/)?)?(bullet-brawl-([a-z]+)-(\d{2})-(\d{4})-\d{5,})')
_MONTHS = {m: i for i, m in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'), start=1)}

STANDINGS_COLUMNS = ('tournament_slug', 'date', 'username', 'display_name', 'rank', 'score', 'tie_break',
                     'wins', 'draws', 'losses', 'games', 'longest_streak', 'avg_rating',
                     'first_game_ts', 'last_game_ts')


@dataclass
class ParsedEvent:
    rows: list[dict]
    points_mismatches: int = 0
    missing_rank: int = 0
    notes: list[str] = field(default_factory=list)


def get_json(session: requests.Session, url: str, attempts: int = 5) -> dict | None:
    """GET a JSON document, backing off on rate limits. Returns None on 404."""
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=120)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if response.status_code == 404:
            return None
        if response.status_code in (429, 403) or response.status_code >= 500:
            time.sleep(15 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f'Gave up on {url} after {attempts} attempts')


def _win_points(streak: int) -> int:
    return _WIN_POINTS[streak - 1] if streak <= len(_WIN_POINTS) else _STREAK_POINTS


def tie_break_value(longest_streak: int, wins: int, losses: int) -> float:
    """Chess.com's tie order as one number: longest streak, most wins, fewest losses."""
    return longest_streak * 100 + wins - losses / 1000


def _player_totals(games: list[tuple]) -> dict:
    wins = draws = losses = streak = longest = points = 0
    for _end, result, _rating, _name in sorted(games, key=lambda g: g[0]):
        if result == 'win':
            wins += 1
            streak += 1
            longest = max(longest, streak)
            points += _win_points(streak)
        else:
            streak = 0
            if result in DRAW_RESULTS:
                draws += 1
                points += 1
            else:
                losses += 1
    ratings = [g[2] for g in games if g[2]]
    return {'wins': wins, 'draws': draws, 'losses': losses, 'games': len(games), 'longest_streak': longest,
            'points': points, 'avg_rating': sum(ratings) / len(ratings) if ratings else None,
            'first_game_ts': min((g[0] for g in games), default=None),
            'last_game_ts': max((g[0] for g in games), default=None)}


def parse_round(slug: str, event_date: str, payload: dict) -> ParsedEvent:
    """Build one standings row per player listed in the round."""
    by_player: dict[str, list[tuple]] = defaultdict(list)
    for game in payload.get('games', []):
        for colour in ('white', 'black'):
            side = game[colour]
            by_player[side['username'].lower()].append(
                (game.get('end_time', 0), side.get('result'), side.get('rating'), side['username']))

    parsed = ParsedEvent(rows=[])
    for player in payload.get('players', []):
        username = player['username'].lower()
        games = by_player.get(username, [])
        totals = _player_totals(games)
        score = float(player.get('points', 0))
        rank = player.get('place_finish')
        if rank is None:
            parsed.missing_rank += 1
        if games and totals['points'] != score:
            parsed.points_mismatches += 1
        parsed.rows.append({
            'tournament_slug': slug,
            'date': f'{event_date} 00:00:00+00:00',
            'username': games[0][3] if games else player['username'],
            'display_name': games[0][3] if games else player['username'],
            'rank': None if rank is None else float(rank),
            'score': score,
            'tie_break': tie_break_value(totals['longest_streak'], totals['wins'], totals['losses']),
            'wins': float(totals['wins']),
            'draws': float(totals['draws']),
            'losses': float(totals['losses']),
            'games': totals['games'],
            'longest_streak': totals['longest_streak'],
            'avg_rating': totals['avg_rating'],
            'first_game_ts': totals['first_game_ts'],
            'last_game_ts': totals['last_game_ts'],
        })
    return parsed


def parse_standings_page(page_html: str, slug: str, event_date: str) -> list[dict]:
    """Rows from one page of an event's standings, for events the round API can't serve."""
    rows = []
    soup = BeautifulSoup(page_html, 'html.parser')
    for cell in soup.find_all('td', class_=re.compile('results-user-cell')):
        row = cell.find_parent('tr')
        text = re.sub(r'\s+', ' ', row.get_text(' ', strip=True))
        rank = re.search(r'#(\d+)', text)
        popover = row.find(attrs={'v-user-popover': True})
        name = re.search(r'"username":"([^"]+)"', popover['v-user-popover']) if popover else None
        score = row.find(class_='tournaments-live-view-total-score')
        if not (rank and name and score):
            continue
        # The streak is shown only for players with a win, and not at all on 2023 pages.
        stats = re.search(r'(?:(\d+) / )?(\d+) W / (\d+) D / (\d+) L', text)
        streak, wins, draws, losses = (int(v or 0) for v in stats.groups()) if stats else (0, 0, 0, 0)
        rows.append({
            'tournament_slug': slug,
            'date': f'{event_date} 00:00:00+00:00',
            'username': name.group(1),
            'display_name': name.group(1),
            'rank': float(rank.group(1)),
            'score': float(score.get_text(strip=True)),
            'tie_break': tie_break_value(streak, wins, losses),
            'wins': float(wins), 'draws': float(draws), 'losses': float(losses),
            'games': wins + draws + losses,
            'longest_streak': streak,
            'avg_rating': None, 'first_game_ts': None, 'last_game_ts': None,
        })
    return rows


def player_page_count(page_html: str) -> int:
    """The standings pager is the smaller of the page's two pagers (the other lists games)."""
    counts = [int(v) for v in re.findall(r'data-total-pages="(\d+)"', page_html)]
    return min(counts) if counts else 1


def fetch_standings_pages(session: requests.Session, url: str, slug: str, event_date: str,
                          pause: float = 1.5) -> ParsedEvent:
    """Read every standings page. Raises if a page fails, so a partial field is never stored."""
    rows: list[dict] = []
    seen: set[str] = set()
    page, last = 1, 1
    while page <= last:
        response = session.get(f'{url}?&players={page}', timeout=60, headers=PAGE_HEADERS)
        response.raise_for_status()
        if page == 1:
            last = player_page_count(response.text)
        new = [r for r in parse_standings_page(response.text, slug, event_date)
               if r['username'].lower() not in seen]
        if not new:
            raise RuntimeError(f'{slug}: standings page {page} of {last} had no new players')
        seen.update(r['username'].lower() for r in new)
        rows.extend(new)
        page += 1
        time.sleep(pause)
    return ParsedEvent(rows=rows)


def known_usernames(conn) -> dict[str, str]:
    """lowercase username → the capitalization already on record.

    Titled Tuesday standings win (most recent appearance last), then
    player_information, then earlier Bullet Brawl rows. Chess.com usernames are
    case-insensitive, so this keeps one spelling per account across tables.
    """
    from .db import table_columns
    known: dict[str, str] = {}
    if table_columns(conn, 'bullet_brawl_standings'):
        for (u,) in conn.execute('SELECT username FROM bullet_brawl_standings WHERE games > 0 '
                                 'GROUP BY username ORDER BY MAX(date)').fetchall():
            known[u.lower()] = u
    if table_columns(conn, 'player_information'):
        for (u,) in conn.execute('SELECT username FROM player_information').fetchall():
            if u.lower() not in known or u != u.lower():    # never let a lowercase copy win
                known[u.lower()] = u
    if table_columns(conn, 'titled_tuesday_standings'):
        for (u,) in conn.execute('SELECT username FROM titled_tuesday_standings '
                                 'GROUP BY username ORDER BY MAX(date)').fetchall():
            known[u.lower()] = u
    return known


def canonicalize(rows: list[dict], known: dict[str, str]) -> None:
    """Give each row the account's recorded capitalization, in place."""
    for row in rows:
        row['username'] = known.get(row['username'].lower(), row['username'])


def write_event(conn, slug: str, event_date: str, details: dict, parsed: ParsedEvent) -> None:
    """Replace one event's rows. The caller's connection context commits."""
    canonicalize(parsed.rows, known_usernames(conn))
    conn.execute('DELETE FROM bullet_brawl_standings WHERE tournament_slug = ?', (slug,))
    conn.execute('DELETE FROM bullet_brawl_tournaments WHERE tournament_slug = ?', (slug,))
    winner = next((r['username'] for r in parsed.rows if r['rank'] == 1), None)
    conn.execute(
        'INSERT INTO bullet_brawl_tournaments (tournament_slug, date, start_ts, finish_ts, num_players, '
        'num_games, winner, url, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (slug, f'{event_date} 00:00:00+00:00', details.get('start_time'), details.get('finish_time'),
         len(parsed.rows), sum(r['games'] for r in parsed.rows) // 2, winner, details.get('url'),
         datetime.now(timezone.utc).isoformat(timespec='seconds')),
    )
    marks = ', '.join('?' * len(STANDINGS_COLUMNS))
    conn.executemany(
        f'INSERT INTO bullet_brawl_standings ({", ".join(STANDINGS_COLUMNS)}) VALUES ({marks})',
        [tuple(row[c] for c in STANDINGS_COLUMNS) for row in parsed.rows],
    )


# ── Weekly discovery ─────────────────────────────────────────────────────────

def slug_from_url(url_or_slug: str) -> str:
    """Accept a chess.com URL (live, arena or API form) or a bare slug."""
    m = EVENT_SLUG.search(url_or_slug)
    if m:
        return m.group(1)
    bare = url_or_slug.strip().strip('/').rsplit('/', 1)[-1].split('?')[0]
    if not re.fullmatch(r'bullet-brawl-[a-z0-9-]+-\d{5,}', bare):
        raise ValueError(f'Not a Bullet Brawl tournament URL or slug: {url_or_slug!r}')
    return bare


def slug_date(slug: str) -> date | None:
    """The date written in the slug, e.g. bullet-brawl-november-07-2026-31401539 → 2026-11-07."""
    m = EVENT_SLUG.search(f'/tournament/{slug}')
    if not m or m.group(2) not in _MONTHS:
        return None
    return date(int(m.group(4)), _MONTHS[m.group(2)], int(m.group(3)))


def slugs_in_archive(archive: dict | None) -> set[str]:
    """Bullet Brawl slugs referenced by a player's monthly game archive."""
    found = set()
    for game in (archive or {}).get('games', []):
        m = EVENT_SLUG.search(game.get('tournament') or '')
        if m:
            found.add(m.group(1))
    return found


def event_day(start_time: int | None) -> str | None:
    """The ET calendar date an event started on."""
    if not start_time:
        return None
    return datetime.fromtimestamp(start_time, timezone.utc).astimezone(ET).date().isoformat()
