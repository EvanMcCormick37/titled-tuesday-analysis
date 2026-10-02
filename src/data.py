"""
Data loading and player-pool construction.

Reads from the configured database (SQLite locally, Postgres on Railway) via
src.db.get_conn / get_engine. All MC-ready arrays come from
load_and_prepare() → build_player_pool*().
"""

import joblib
import numpy as np
import pandas as pd

from .config import (
    MODELS_DIR,
    DATA_CUTOFF, SKILL_DECAY, PARTICIPATION_DECAY, MIN_PARTICIPATION_RATE,
    SEASON_SHIFT_DATE, SEASON_SHIFT_FACTOR,
)
from .db import get_conn, get_engine, table_columns


# ── Username / player-name mapping ────────────────────────────────────────────

# Alternative player names used on betting lines that differ from player_information.
# Maps alias → canonical player_name in player_information.
_PLAYER_ALIASES:dict[str, str] = {
    'Jose Martinez':'Jose Martinez Alcantara',
    'Liem Le':'Le Quang Liem',
    'Le Minh Tuan':'Le Tuan Minh',
    'Tuan Minh Le':'Le Tuan Minh',
    'Alexey Sarana':'Aleksei Sarana',
    'Aleksej Sarana':'Aleksei Sarana',
    'Hans Moke Niemann':'Hans Niemann',
    'R Praggnanandhaa':'Praggnanandhaa Rameshbabu',
    'Tobias Koelle':'Tobias Kölle',
    'Jagadeesh Siddharth':'Siddarth Jagadeesh',
    'Sarin Nihal':'Nihal Sarin',
    'Liren Ding':'Ding Liren',
    'Haik M. Martirosyan':'Haik Martirosyan',
    'A.R. Saleh Salem':'Salem Saleh',
    'Saleh Salem':'Salem Saleh',
    'David Anton Guijarro':'David Anton',
    'Khuong Duy Dau':'Dau Khuong Duy',
    'Bilguun Sumiya':'Sumiya Bilguun',
    'M. Amin Tabatabaei':'Seyyed Mohammad Amin Tabatabaei',
    'Mohammad Amin Tabatabaei':'Seyyed Mohammad Amin Tabatabaei',
    'Amin Tabatabaei':'Seyyed Mohammad Amin Tabatabaei',
    'Christopher Yoo':'Christopher Woojin Yoo',
    'Chris Yoo':'Christopher Woojin Yoo',
    'V Pranav':'Pranav Venkatesh',
    'Pranesh M':'Pranesh Munirethinam',
    "D Gukesh": "Gukesh D",
}

_USERNAME_TO_PLAYER:dict | None = None
_PLAYER_TO_USERNAME:dict | None = None


def _populate_is_default(conn) -> None:
    """Set is_default=1 for each player's most-recently-seen TT username, 0 for all others."""
    conn.execute('UPDATE player_information SET is_default = 0')
    latest = dict(conn.execute(
        'SELECT username, MAX(date) FROM titled_tuesday_standings GROUP BY username'
    ).fetchall())
    rows = conn.execute(
        'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL'
    ).fetchall()
    groups:dict[str, list] = {}
    for u, p in rows:
        groups.setdefault(p, []).append(u)
    for p, usernames in groups.items():
        dated = [(latest.get(u), u) for u in usernames if latest.get(u)]
        default_u = max(dated)[1] if dated else usernames[0]
        conn.execute('UPDATE player_information SET is_default = 1 WHERE username = ?', (default_u,))
    conn.commit()


def _ensure_is_default(conn) -> None:
    """Add is_default column to player_information and populate it if not present."""
    if 'is_default' not in table_columns(conn, 'player_information'):
        conn.execute('ALTER TABLE player_information ADD COLUMN is_default INTEGER DEFAULT 0')
        conn.commit()
        _populate_is_default(conn)


def refresh_default_usernames() -> None:
    """Re-compute is_default flags from current TT standings. Call after new data is loaded."""
    global _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME
    with get_conn() as conn:
        _ensure_is_default(conn)
        _populate_is_default(conn)
    _USERNAME_TO_PLAYER = None
    _PLAYER_TO_USERNAME = None


def resolve_player_names(names: list[str] | dict[str, float]) -> list[str] | dict[str, float]:
    """Resolve player names to their canonical DB forms.

    Resolution order for each name:
      1. Exact match in _PLAYER_ALIASES → return the alias target.
      2. Already recognised by player_to_username → return unchanged.
      3. Word-set match: find canonical DB names whose words are all present
         in the input name (any order, case-insensitive, input may have extra
         words).  Picks the most specific match (most words); warns on ties.
    """
    username_to_player, player_to_username = get_username_mappings()
    canonical_names = set(username_to_player.values())

    def _resolve_one(name: str) -> str:
        if name in _PLAYER_ALIASES:
            return _PLAYER_ALIASES[name]
        if name in player_to_username:
            return name
        input_words = frozenset(name.lower().split())
        matches = [cn for cn in canonical_names if frozenset(cn.lower().split()) <= input_words]
        if not matches:
            return name
        best_len = max(len(cn.split()) for cn in matches)
        best = [cn for cn in matches if len(cn.split()) == best_len]
        if len(best) > 1:
            print(f'  Warning: {name!r} matches multiple DB names {best} — returning unchanged')
            return name
        if best[0] != name:
            print(f'  Resolved {name!r} → {best[0]!r} (word-set match)')
        return best[0]

    if isinstance(names, dict):
        return {_resolve_one(k): v for k, v in names.items()}
    return [_resolve_one(n) for n in names]


def get_username_mappings() -> tuple[dict, dict]:
    """Return (username→player_name, player_name→default_username) dicts, loaded lazily from DB.

    username→player_name maps ALL known usernames (including alt/closed accounts).
    player_name→username maps to the DEFAULT username (most-recently-seen in TT standings).
    Aliases in _PLAYER_ALIASES are injected so betting-line names resolve correctly.
    """
    global _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME
    if _USERNAME_TO_PLAYER is None:
        with get_conn() as conn:
            _ensure_is_default(conn)
            rows = conn.execute(
                'SELECT username, player_name, is_default FROM player_information WHERE player_name IS NOT NULL'
            ).fetchall()
        _USERNAME_TO_PLAYER = {u:p for u, p, _ in rows}
        # Build PLAYER_TO_USERNAME:prefer is_default=1; fall back to last row for the name
        _PLAYER_TO_USERNAME = {}
        for u, p, is_def in rows:
            if p not in _PLAYER_TO_USERNAME or is_def:
                _PLAYER_TO_USERNAME[p] = u
        # Inject aliases so betting-line names (e.g. 'Jose Martinez') also resolve
        for alias, canonical in _PLAYER_ALIASES.items():
            if canonical in _PLAYER_TO_USERNAME:
                _PLAYER_TO_USERNAME[alias] = _PLAYER_TO_USERNAME[canonical]
    return _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME


# ── Attendance-conflict Z_part reduction ──────────────────────────────────────

def _conflict_z_deduction(conn, ref: pd.Timestamp,
                          use_season_shift: bool,
                          min_date: str | None) -> dict[str, float]:
    """Return dict[username → weight to subtract from Z_part].

    For each (username, conflict_date) in attendance_conflicts, compute the
    same participation weight the numerator would have received had the player
    attended: PARTICIPATION_DECAY**k with an optional SEASON_SHIFT_FACTOR
    multiplier for pre-shift dates.  Summed across conflicts per user.
    """
    try:
        rows = conn.execute(
            'SELECT username, date FROM attendance_conflicts'
        ).fetchall()
    except Exception:
        print('  Warning: attendance_conflicts table missing — run '
              'scripts/build_attendance_conflicts.py to enable conflict-aware p_participate')
        return {}

    if not rows:
        return {}

    shift_ts = (pd.Timestamp(SEASON_SHIFT_DATE)
                if use_season_shift and SEASON_SHIFT_DATE is not None else None)
    min_ts = pd.Timestamp(min_date) if min_date else None
    cutoff_ts = pd.Timestamp(DATA_CUTOFF)

    dedu: dict[str, float] = {}
    for username, dstr in rows:
        d = pd.Timestamp(dstr)
        if d >= ref or d < cutoff_ts:
            continue
        if min_ts is not None and d < min_ts:
            continue
        k = (ref - d) // pd.Timedelta(weeks=1)
        w = PARTICIPATION_DECAY ** k
        if shift_ts is not None and d < shift_ts:
            w *= SEASON_SHIFT_FACTOR
        dedu[username] = dedu.get(username, 0.0) + w
    return dedu


# ── Main data loader ──────────────────────────────────────────────────────────

def load_and_prepare(scheduling_conflict=None, cut_players=None, keep_players=None,
                     canonical_accounts=None, account_groups=None,
                     top_player_threshold=0.10, min_obs_opportunism=20,
                     as_of=None, attendance_start='2025-09-02'):
    """
    Load standings from DB and compute per-player MC inputs.

    Attendance adjustments (cut_players, keep_players, p_participate_overrides)
    are handled by the caller (run_adjusted in pipeline.py) after this
    function returns.  The legacy scheduling parameters below are kept for
    backward compatibility with the backtest pipeline.

    Parameters
    ----------
    scheduling_conflict  [legacy] Player names with a broadcast conflict.
    cut_players          Player names forced to p_participate = 0.
    keep_players         Player names forced to p_participate = 1.0.
    canonical_accounts   Maps closed/alt username → active canonical username.
    account_groups       Maps canonical username → list of all accounts.
    as_of                If set, restrict data to before this date (backtest).
    attendance_start     If set, p_participate is computed only from rows with
                         date >= attendance_start (skill history keeps full
                         DATA_CUTOFF range).  Disables SEASON_SHIFT scaling and
                         the isotonic floor, which were tuned for production.
                         Used by the joint backtest to isolate the single-
                         session era (2025-09-02+).
    """
    if scheduling_conflict is None:
        scheduling_conflict = []
    if cut_players is None:
        cut_players = []
    if keep_players is None:
        keep_players = []

    ref = pd.Timestamp(as_of) if as_of else pd.Timestamp.now()

    engine = get_engine()
    q = f"SELECT * FROM titled_tuesday_standings WHERE date >= '{DATA_CUTOFF}'"
    if as_of:
        q += f" AND substr(CAST(date AS TEXT), 1, 10) < '{ref.date()}'"
    df = pd.read_sql_query(q, engine)
    # Postgres returns TIMESTAMPTZ as tz-aware; SQLite returns strings. Normalize
    # to tz-naive UTC so downstream arithmetic (`ref - df['date']`) is consistent.
    df['date'] = pd.to_datetime(df['date'], format='mixed', utc=True).dt.tz_localize(None)

    df = (
        df.sort_values('rank')
          .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
    )

    time_diff = (ref - df['date']) // pd.Timedelta(weeks=1)
    N = int((ref - pd.Timestamp(DATA_CUTOFF)) / pd.Timedelta(weeks=1))

    df['skill_w'] = SKILL_DECAY          ** time_diff
    df['part_w']  = PARTICIPATION_DECAY  ** time_diff

    if attendance_start is None and SEASON_SHIFT_DATE is not None:
        shift_ts = pd.Timestamp(SEASON_SHIFT_DATE)
        df.loc[df['date'] < shift_ts, 'part_w'] *= SEASON_SHIFT_FACTOR

    n_in_event  = df.groupby('tournament_slug')['username'].transform('count')
    df['rank_pct'] = 1.0 - (df['rank'].astype(float) - 1) / (n_in_event - 1)

    # ── Conflict-week weights (per-user Z_part reduction) ────────────────────
    # attendance_conflicts holds (date, player_name, username) rows for TT dates
    # where a player had an unavoidable OTB broadcast round.  Those weeks are
    # excluded from BOTH numerator (already absent from standings) and
    # denominator (subtracted from Z_part below), so p_participate reflects the
    # rate of attendance on weeks with no scheduling conflict.
    with get_conn() as _conn:
        conflict_deduction = _conflict_z_deduction(
            _conn, ref,
            use_season_shift=(attendance_start is None),
            min_date=attendance_start,
        )

    # ── Participation calculation ────────────────────────────────────────────
    if attendance_start is not None:
        # Backtest mode: participation rate is derived exclusively from the
        # post-attendance_start window (skill history is unrestricted above).
        att_ref = pd.Timestamp(attendance_start)
        att_mask = df['date'] >= att_ref
        N_att = max(1, int((ref - att_ref) / pd.Timedelta(weeks=1)))
        Z_part = sum(PARTICIPATION_DECAY ** k for k in range(N_att))
        num = df.loc[att_mask].groupby('username')['part_w'].sum()
        Z_user = (Z_part - num.index.to_series().map(conflict_deduction)
                            .fillna(0.0)).clip(lower=1e-6)
        p_participate = (num / Z_user).clip(lower=MIN_PARTICIPATION_RATE, upper=1.0).rename('p_participate')
        # Ensure every player in the skill dataset has a p_participate entry
        p_participate = p_participate.reindex(
            df['username'].unique(), fill_value=MIN_PARTICIPATION_RATE
        )
        n_adj = int((num.index.to_series().map(conflict_deduction).fillna(0.0) > 0).sum())
        print(f'  p_participate: EWMA over {N_att} weeks since {attendance_start} '
              f'(no season-shift, no isotonic floor); {n_adj:,} players had conflict-week deductions')
    else:
        # EWMA baseline participation rate — normalise to what a perfect-attendance
        # player would accumulate under the same weighting scheme (incl. shift).
        if SEASON_SHIFT_DATE is not None:
            shift_ts   = pd.Timestamp(SEASON_SHIFT_DATE)
            n_post     = max(0, int((ref - shift_ts) / pd.Timedelta(weeks=1)))
            print(f"n_post={n_post}")
            Z_part     = (
                sum(PARTICIPATION_DECAY ** k for k in range(n_post))
                + SEASON_SHIFT_FACTOR * sum(PARTICIPATION_DECAY ** k for k in range(n_post, N))
            )
        else:
            Z_part     = sum(PARTICIPATION_DECAY ** k for k in range(N))
        num = df.groupby('username')['part_w'].sum()
        Z_user = (Z_part - num.index.to_series().map(conflict_deduction)
                            .fillna(0.0)).clip(lower=1e-6)
        p_participate = (num / Z_user).clip(lower=MIN_PARTICIPATION_RATE, upper=1.0).rename('p_participate')
        n_adj = int((num.index.to_series().map(conflict_deduction).fillna(0.0) > 0).sum())
        print(f'  p_participate: {n_adj:,} players had conflict-week deductions applied to Z_part')

    # Isotonic regression floor for low-participation players (production only)
    if attendance_start is None:
        iso_path = MODELS_DIR / 'isotonic_regression.joblib'
        if iso_path.exists():
            iso_reg = joblib.load(iso_path)
            p_np    = p_participate.to_numpy()
            iso_floor = np.where(p_np > 0.05, p_np, iso_reg.predict(p_np))
            p_participate = pd.Series(iso_floor, index=p_participate.index,
                                      name='p_participate')
        print('  p_participate: decay-weighted EWMA with isotonic floor (p < 0.05)')

    # ── Legacy scheduling-param path (backward compat for backtest) ──────────
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

    app_counts = df.groupby('username')['tournament_slug'].nunique().rename('appearances')
    print(f'  {N} events | {df["username"].nunique():,} unique players '
          f'| {df["date"].min().date()} to {df["date"].max().date()}')
    return df, p_participate, app_counts


# ── Convenience loaders ───────────────────────────────────────────────────────

def load_standings(since=None) -> pd.DataFrame:
    """Load standings table from DB, optionally filtering by date."""
    q = 'SELECT * FROM titled_tuesday_standings'
    if since:
        q += f" WHERE date >= '{since}'"
    return pd.read_sql_query(q, get_engine(), parse_dates=['date'])


def load_tournaments(since=None) -> pd.DataFrame:
    """Load tournaments table from DB, optionally filtering by date."""
    q = 'SELECT * FROM titled_tuesday_tournaments'
    if since:
        q += f" WHERE date >= '{since}'"
    return pd.read_sql_query(q, get_engine(), parse_dates=['date'])


def load_kalshi_portfolio() -> pd.DataFrame:
    """Load Kalshi portfolio snapshot from DB."""
    return pd.read_sql_query('SELECT * FROM kalshi_portfolio', get_engine())


def load_model_predictions() -> pd.DataFrame:
    """Load latest MC model predictions from DB."""
    return pd.read_sql_query('SELECT * FROM latest_model_predictions', get_engine())


def load_player_usernames() -> pd.DataFrame:
    """Load all player chess.com usernames form DB"""
    return pd.read_sql_query(
        'SELECT DISTINCT username FROM titled_tuesday_standings', get_engine()
    )


def load_backtest(model:str | None = None, since:str | None = None) -> pd.DataFrame:
    """Load joint backtest results (score vs perf-rating). model='score' or 'perf'."""
    q = 'SELECT * FROM backtest'
    wheres = []
    if model:
        wheres.append(f"model = '{model}'")
    if since:
        wheres.append(f"tourn_date >= '{since}'")
    if wheres:
        q += ' WHERE ' + ' AND '.join(wheres)
    return pd.read_sql_query(q, get_engine(), parse_dates=['tourn_date'])
