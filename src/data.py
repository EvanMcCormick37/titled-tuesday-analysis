"""
Data loading and player-pool construction.

Reads from the SQLite database at data/titled_tuesday.db.
All MC-ready arrays come from load_and_prepare() → build_player_pool*().
"""

import sqlite3

import joblib
import numpy as np
import pandas as pd

from .config import (
    DB_PATH, MODELS_DIR,
    DATA_CUTOFF, SKILL_DECAY, PARTICIPATION_DECAY, MIN_PARTICIPATION_RATE,
)


# ── Attendance adjustment helpers ─────────────────────────────────────────────

def _load_active_adjustments(conn: sqlite3.Connection, tourn_date: str) -> tuple[dict, dict]:
    """Load active nudges and overrides from attendance_adjustments for tourn_date.

    Returns (nudges, overrides):
      nudges   = {username: log_odds_delta}  applied before overrides
      overrides = {username: probability}    final value, applied last
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='attendance_adjustments'"
    ).fetchone()
    if not exists:
        return {}, {}

    rows = conn.execute(
        "SELECT username, type, value FROM attendance_adjustments "
        "WHERE tourn_date = ? AND is_active = 1",
        (tourn_date,),
    ).fetchall()

    nudges: dict[str, float] = {}
    overrides: dict[str, float] = {}
    for username, adj_type, value in rows:
        if adj_type == 'nudge':
            nudges[username] = float(value)
        else:
            overrides[username] = float(value)
    return nudges, overrides


def _apply_nudges(p: pd.Series, nudges: dict) -> pd.Series:
    """Shift p_participate in log-odds space by per-player deltas."""
    if not nudges:
        return p
    p = p.copy()
    for username, delta in nudges.items():
        if username not in p.index:
            continue
        pv = float(np.clip(p[username], 1e-7, 1.0 - 1e-7))
        logit = np.log(pv / (1.0 - pv))
        p[username] = float(1.0 / (1.0 + np.exp(-np.clip(logit + delta, -30, 30))))
    return p


def _apply_overrides(p: pd.Series, overrides: dict) -> pd.Series:
    """Set p_participate to exact values, bypassing all model estimates."""
    if not overrides:
        return p
    p = p.copy()
    for username, value in overrides.items():
        if username in p.index:
            p[username] = float(value)
    return p

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

def apply_db_adjustments(p_participate: pd.Series, tourn_date: str) -> pd.Series:
    """Apply active nudges and overrides from attendance_adjustments for tourn_date.

    Nudges are applied first (log-odds shift), then overrides (exact value).
    Returns a new Series; the input is not mutated.
    """
    conn = sqlite3.connect(DB_PATH)
    nudges, overrides = _load_active_adjustments(conn, tourn_date)
    conn.close()
    p = p_participate
    if nudges:
        print(f'  Applying {len(nudges)} nudge(s) (log-odds delta)...')
        p = _apply_nudges(p, nudges)
    if overrides:
        n_cuts = sum(1 for v in overrides.values() if v == 0.0)
        print(f'  Applying {len(overrides)} override(s) '
              f'({n_cuts} cut(s) to 0, {len(overrides) - n_cuts} value(s))...')
        p = _apply_overrides(p, overrides)
    return p


def load_and_prepare(scheduling_conflict=None, cut_players=None, keep_players=None,
                     canonical_accounts=None, account_groups=None,
                     top_player_threshold=0.10, min_obs_opportunism=20,
                     as_of=None, tourn_date=None, apply_adjustments=True):
    """
    Load standings from DB and compute per-player MC inputs.

    When tourn_date is provided, attendance adjustments are read from the
    attendance_adjustments DB table:
      - 'nudge' rows    : shift p_participate in log-odds space (applied first)
      - 'override' rows : set p_participate to an exact value (applied last)

    When tourn_date is not provided, falls back to the legacy scheduling
    parameters (scheduling_conflict, cut_players, keep_players) which call
    src.attendance.apply_schedule_adjustments().  This path is preserved for
    backward compatibility with the backtest pipeline and old notebooks.

    Parameters
    ----------
    tourn_date           ISO date string for the tournament being predicted.
                         When set, adjustments come from the DB; legacy params
                         are ignored.
    scheduling_conflict  [legacy] Player names with a broadcast conflict.
    cut_players          [legacy] Player names forced to p_participate = 0.
    keep_players         [legacy] Player names forced to p_participate = 1.0.
    canonical_accounts   Maps closed/alt username → active canonical username.
    account_groups       Maps canonical username → list of all accounts.
    as_of                If set, restrict data to before this date (backtest).
    """
    if scheduling_conflict is None:
        scheduling_conflict = []
    if cut_players is None:
        cut_players = []
    if keep_players is None:
        keep_players = []

    ref = pd.Timestamp(as_of) if as_of else pd.Timestamp.now()

    conn = sqlite3.connect(DB_PATH)
    q = f"SELECT * FROM titled_tuesday_standings WHERE date >= '{DATA_CUTOFF}'"
    if as_of:
        q += f" AND date(date) < '{ref.date()}'"
    df = pd.read_sql_query(q, conn)
    df['date'] = pd.to_datetime(df['date'], format='mixed')

    df = (
        df.sort_values('rank')
          .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
    )

    time_diff = (ref - df['date']) // pd.Timedelta(weeks=1)
    N = int((ref - pd.Timestamp(DATA_CUTOFF)) / pd.Timedelta(weeks=1))

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

    if tourn_date:
        if apply_adjustments:
            p_participate = apply_db_adjustments(p_participate, tourn_date)
    else:
        # ── Legacy scheduling-param path (backward compat) ───────────────────
        from .attendance import apply_schedule_adjustments
        _, PLAYER_TO_USERNAME = get_username_mappings()
        keep_users = [PLAYER_TO_USERNAME[p] for p in keep_players if p in PLAYER_TO_USERNAME]
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

    conn.close()
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
