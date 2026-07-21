"""
Data loading and player-pool construction.

Reads from the SQLite database at data/titled_tuesday.db.
All MC-ready arrays come from load_and_prepare() → build_player_pool*().
"""

import sqlite3
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from .config import (
    DB_PATH, MODELS_DIR,
    DATA_CUTOFF, SKILL_DECAY, PARTICIPATION_DECAY, MIN_PARTICIPATION_RATE,
)

# ── Username / player-name mapping ────────────────────────────────────────────

# Alternative player names used on betting lines that differ from player_information.
# Maps alias → canonical player_name in player_information.
_PLAYER_ALIASES: dict[str, str] = {
    'Jose Martinez': 'Jose Martinez Alcantara',
}

_USERNAME_TO_PLAYER: dict | None = None
_PLAYER_TO_USERNAME: dict | None = None


def _populate_is_default(conn: sqlite3.Connection) -> None:
    """Set is_default=1 for each player's most-recently-seen TT username, 0 for all others."""
    conn.execute('UPDATE player_information SET is_default = 0')
    latest = dict(conn.execute(
        'SELECT username, MAX(date) FROM titled_tuesday_standings GROUP BY username'
    ).fetchall())
    rows = conn.execute(
        'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL'
    ).fetchall()
    groups: dict[str, list] = {}
    for u, p in rows:
        groups.setdefault(p, []).append(u)
    for p, usernames in groups.items():
        dated = [(latest.get(u), u) for u in usernames if latest.get(u)]
        default_u = max(dated)[1] if dated else usernames[0]
        conn.execute('UPDATE player_information SET is_default = 1 WHERE username = ?', (default_u,))
    conn.commit()


def _ensure_is_default(conn: sqlite3.Connection) -> None:
    """Add is_default column to player_information and populate it if not present."""
    cols = {row[1] for row in conn.execute('PRAGMA table_info(player_information)')}
    if 'is_default' not in cols:
        conn.execute('ALTER TABLE player_information ADD COLUMN is_default INTEGER DEFAULT 0')
        conn.commit()
        _populate_is_default(conn)


def refresh_default_usernames() -> None:
    """Re-compute is_default flags from current TT standings. Call after new data is loaded."""
    global _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME
    conn = sqlite3.connect(DB_PATH)
    _ensure_is_default(conn)
    _populate_is_default(conn)
    conn.close()
    _USERNAME_TO_PLAYER = None
    _PLAYER_TO_USERNAME = None


def get_username_mappings() -> tuple[dict, dict]:
    """Return (username→player_name, player_name→default_username) dicts, loaded lazily from DB.

    username→player_name maps ALL known usernames (including alt/closed accounts).
    player_name→username maps to the DEFAULT username (most-recently-seen in TT standings).
    Aliases in _PLAYER_ALIASES are injected so betting-line names resolve correctly.
    """
    global _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME
    if _USERNAME_TO_PLAYER is None:
        conn = sqlite3.connect(DB_PATH)
        _ensure_is_default(conn)
        rows = conn.execute(
            'SELECT username, player_name, is_default FROM player_information WHERE player_name IS NOT NULL'
        ).fetchall()
        conn.close()
        _USERNAME_TO_PLAYER = {u: p for u, p, _ in rows}
        # Build PLAYER_TO_USERNAME: prefer is_default=1; fall back to last row for the name
        _PLAYER_TO_USERNAME = {}
        for u, p, is_def in rows:
            if p not in _PLAYER_TO_USERNAME or is_def:
                _PLAYER_TO_USERNAME[p] = u
        # Inject aliases so betting-line names (e.g. 'Jose Martinez') also resolve
        for alias, canonical in _PLAYER_ALIASES.items():
            if canonical in _PLAYER_TO_USERNAME:
                _PLAYER_TO_USERNAME[alias] = _PLAYER_TO_USERNAME[canonical]
    return _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME


# ── Main data loader ──────────────────────────────────────────────────────────

def load_and_prepare(scheduling_conflict=None, cut_players=None, keep_players=None,
                     canonical_accounts=None, account_groups=None,
                     top_player_threshold=0.10, min_obs_opportunism=20):
    """
    Load standings from DB and compute per-player MC inputs.

    Attendance model: decay-weighted EWMA + isotonic floor, then schedule
    adjustments via src.attendance.apply_schedule_adjustments():
      - cut_players        : unavoidable conflict → p_participate = 0
      - scheduling_conflict: broadcast-conflict cap (historical conflict-date rate)
      - non-conflicted players: opportunism boost (OLS slope × n_top_conflicted)

    Parameters
    ----------
    scheduling_conflict  Player names with a broadcast round on TT Tuesday.
    cut_players          Player names with an unavoidable conflict (p_participate=0).
    keep_players         Player names forced to p_participate = 1.0.
    canonical_accounts   Maps closed/alt username → active canonical username.
    account_groups       Maps canonical username → list of all accounts.
    top_player_threshold P_top10_given_play threshold for opportunism 'top player'.
    min_obs_opportunism  Min non-conflict TT dates needed to compute a slope.
    """
    from .attendance import apply_schedule_adjustments

    if scheduling_conflict is None:
        scheduling_conflict = []
    if cut_players is None:
        cut_players = []
    if keep_players is None:
        keep_players = []

    _, PLAYER_TO_USERNAME = get_username_mappings()
    keep_users = [PLAYER_TO_USERNAME[p] for p in keep_players if p in PLAYER_TO_USERNAME]

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"SELECT * FROM titled_tuesday_standings WHERE date >= '{DATA_CUTOFF}'",
        conn,
    )
    df['date'] = pd.to_datetime(df['date'], format='mixed')
    conn.close()

    df = (
        df.sort_values('rank')
          .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
    )

    time_diff = (pd.Timestamp.now() - pd.to_datetime(df['date'], format='mixed')) // pd.Timedelta(weeks=1)
    N = (datetime.now() - datetime(2022, 2, 8)) // pd.Timedelta(weeks=1)

    df['skill_w'] = SKILL_DECAY          ** time_diff
    df['part_w']  = PARTICIPATION_DECAY  ** time_diff

    n_in_event  = df.groupby('tournament_slug')['username'].transform('count')
    df['rank_pct'] = 1.0 - (df['rank'].astype(float) - 1) / (n_in_event - 1)

    # EWMA baseline participation rate
    Z_part        = sum(PARTICIPATION_DECAY ** k for k in range(N))
    p_participate = (
        df.groupby('username')['part_w'].sum() / Z_part
    ).clip(lower=MIN_PARTICIPATION_RATE).rename('p_participate')

    # Isotonic regression floor for low-participation players
    iso_path = MODELS_DIR / 'isotonic_regression.joblib'
    if iso_path.exists():
        iso_reg = joblib.load(iso_path)
        p_np    = p_participate.to_numpy()
        iso_floor = np.where(p_np > 0.05, p_np, iso_reg.predict(p_np))
        p_participate = pd.Series(iso_floor, index=p_participate.index,
                                  name='p_participate')

    print('  p_participate: decay-weighted EWMA with isotonic floor (p < 0.05)')

    # Force keep_players to attend, then apply schedule adjustments
    p_participate[p_participate.index.isin(keep_users)] = 1.0

    p_participate = apply_schedule_adjustments(
        p_participate,
        scheduling_conflict,
        cut_players=cut_players,
        canonical_accounts=canonical_accounts,
        account_groups=account_groups,
        top_player_threshold=top_player_threshold,
        min_obs_opportunism=min_obs_opportunism,
    )

    app_counts = df.groupby('username')['tournament_slug'].nunique().rename('appearances')
    print(f'  {N} events | {df["username"].nunique():,} unique players '
          f'| {df["date"].min().date()} to {df["date"].max().date()}')
    return df, p_participate, app_counts


# ── Convenience loaders ───────────────────────────────────────────────────────

def load_standings(since=None) -> pd.DataFrame:
    """Load standings table from DB, optionally filtering by date."""
    conn = sqlite3.connect(DB_PATH)
    q    = 'SELECT * FROM titled_tuesday_standings'
    if since:
        q += f" WHERE date >= '{since}'"
    df = pd.read_sql_query(q, conn, parse_dates=['date'])
    conn.close()
    return df


def load_tournaments(since=None) -> pd.DataFrame:
    """Load tournaments table from DB, optionally filtering by date."""
    conn = sqlite3.connect(DB_PATH)
    q    = 'SELECT * FROM titled_tuesday_tournaments'
    if since:
        q += f" WHERE date >= '{since}'"
    df = pd.read_sql_query(q, conn, parse_dates=['date'])
    conn.close()
    return df


def load_kalshi_portfolio() -> pd.DataFrame:
    """Load Kalshi portfolio snapshot from DB."""
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query('SELECT * FROM kalshi_portfolio', conn)
    conn.close()
    return df


def load_model_predictions() -> pd.DataFrame:
    """Load latest MC model predictions from DB."""
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query('SELECT * FROM latest_model_predictions', conn)
    conn.close()
    return df

def load_player_usernames() -> pd.DataFrame:
    """Load all player chess.com usernames form DB"""
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query("SELECT DISTINCT username FROM titled_tuesday_standings", conn)
    conn.close()
    return df


def load_backtest_results(model: str | None = None) -> pd.DataFrame:
    """Load backtest results from DB. Pass model='decay' etc. to filter by model."""
    conn = sqlite3.connect(DB_PATH)
    q    = 'SELECT * FROM backtest_results'
    if model:
        q += f" WHERE model = '{model}'"
    df = pd.read_sql_query(q, conn, parse_dates=['date'])
    conn.close()
    return df
