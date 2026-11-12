"""
Data loading and player-pool construction.

Reads from the configured database (SQLite locally, Postgres on Railway) via
src.db.get_conn / get_engine. All MC-ready arrays come from
load_and_prepare() → build_player_pool*().
"""

import pandas as pd

from .db import get_conn, get_engine, read_market_rows, table_columns
from .markets import TITLED_TUESDAY, MarketSpec


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
    # Bullet Brawl contract names
    'Alexandre Edisherashvili':'Aleksandre Edisherashvili',
    'Johnatan Bakalchuk':'Johnathan Bakalchuk',
    'Aleksandr Logachiov':'Alexander Logachiov',
    'Brayan Steven Chavez':'Brayan Stiven Chavez Duero',
    'Witold Lechowski':'Witek Lechowski',
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


# ── Per-market name → username ────────────────────────────────────────────────

_MARKET_MAP_TTL_SECONDS = 600
_market_latest_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _norm_words(name: str) -> frozenset[str]:
    """Lowercase words with diacritics and punctuation removed."""
    import re
    import unicodedata
    plain = ''.join(c for c in unicodedata.normalize('NFD', name or '') if unicodedata.category(c) != 'Mn')
    return frozenset(re.findall(r"[a-z0-9]+", plain.lower()))


def _latest_appearance(spec: MarketSpec) -> dict[str, str]:
    """username → latest event date in this market's standings. Cached for ten minutes."""
    import time
    cached = _market_latest_cache.get(spec.slug)
    if cached and time.monotonic() - cached[0] < _MARKET_MAP_TTL_SECONDS:
        return cached[1]
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT username, MAX(CAST(date AS TEXT)) FROM {spec.model.standings_table} GROUP BY username'
        ).fetchall()
    latest = {u: str(d) for u, d in rows}
    _market_latest_cache[spec.slug] = (time.monotonic(), latest)
    return latest


def market_player_to_username(usernames, spec: MarketSpec = TITLED_TUESDAY) -> dict[str, str]:
    """Player name → the account that player uses in this market.

    A player can have several chess.com accounts and use a different one per
    event, so the global default (most recent in Titled Tuesday) is not enough.
    Only usernames in `usernames` (normally the prediction index) are candidates;
    among a player's candidates the one seen most recently in this market wins,
    then the global default. Aliases are added, and a candidate with no known
    display name maps from its own username.
    """
    pool = set(usernames)
    latest = _latest_appearance(spec)
    with get_conn() as conn:
        _ensure_is_default(conn)
        rows = conn.execute(
            'SELECT username, player_name, is_default FROM player_information WHERE player_name IS NOT NULL'
        ).fetchall()
    best: dict[str, tuple[tuple, str]] = {}
    named: set[str] = set()
    for username, name, is_default in rows:
        if username not in pool:
            continue
        named.add(username)
        key = (latest.get(username, ''), bool(is_default), username)
        if name not in best or key > best[name][0]:
            best[name] = (key, username)
    out = {name: u for name, (_, u) in best.items()}
    for alias, canonical in _PLAYER_ALIASES.items():
        if canonical in out and alias not in out:
            out[alias] = out[canonical]
    for username in pool - named:
        out.setdefault(username, username)
    return out


def match_player(name: str, name_to_username: dict[str, str]) -> str | None:
    """Look up a betting-line name, falling back to an unambiguous word-set match.

    Kalshi often drops or adds a name part ("Renato Terry" for "Renato Terry
    Celis"). A fallback match needs at least two shared words, every word of the
    shorter name present in the longer one, and exactly one such candidate.
    """
    if name in name_to_username:
        return name_to_username[name]
    words = _norm_words(name)
    if len(words) < 2:
        return None
    hits = {u for cand, u in name_to_username.items()
            if len(cw := _norm_words(cand)) >= 2 and (cw <= words or words <= cw)}
    return hits.pop() if len(hits) == 1 else None


def invalidate_market_maps() -> None:
    _market_latest_cache.clear()


# ── Attendance-conflict Z_part reduction ──────────────────────────────────────

def _conflict_z_deduction(conn, ref: pd.Timestamp,
                          use_season_shift: bool,
                          min_date: str | None,
                          spec: MarketSpec = TITLED_TUESDAY) -> dict[str, float]:
    """Return dict[username → weight to subtract from Z_part].

    For each (username, conflict_date) in attendance_conflicts, compute the
    same participation weight the numerator would have received had the player
    attended: participation_decay**k with an optional season_shift_factor
    multiplier for pre-shift dates.  Summed across conflicts per user.
    """
    model = spec.model
    try:
        rows = conn.execute(
            'SELECT username, date FROM attendance_conflicts WHERE market = ?',
            (spec.slug,),
        ).fetchall()
    except Exception:
        print('  Warning: attendance_conflicts table missing — run '
              'scripts/build_attendance_conflicts.py to enable conflict-aware p_participate')
        return {}

    if not rows:
        return {}

    shift_ts = (pd.Timestamp(model.season_shift_date)
                if use_season_shift and model.season_shift_date is not None else None)
    min_ts = pd.Timestamp(min_date) if min_date else None
    cutoff_ts = pd.Timestamp(model.data_cutoff)

    dedu: dict[str, float] = {}
    for username, dstr in rows:
        d = pd.Timestamp(dstr)
        if d >= ref or d < cutoff_ts:
            continue
        if min_ts is not None and d < min_ts:
            continue
        k = (ref - d) // pd.Timedelta(weeks=1)
        w = model.participation_decay ** k
        if shift_ts is not None and d < shift_ts:
            w *= model.season_shift_factor
        dedu[username] = dedu.get(username, 0.0) + w
    return dedu


# ── Main data loader ──────────────────────────────────────────────────────────

def load_and_prepare(scheduling_conflict=None, cut_players=None, keep_players=None,
                     canonical_accounts=None, account_groups=None,
                     top_player_threshold=0.10, min_obs_opportunism=20,
                     as_of=None, attendance_start=None,
                     spec: MarketSpec = TITLED_TUESDAY):
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
                         DATA_CUTOFF range).  Also disables SEASON_SHIFT scaling.
                         Used by the joint backtest to isolate the single-
                         session era (2025-09-02+). Leave unset in production.
    spec                 The market whose standings and settings to use.
    """
    model = spec.model

    if scheduling_conflict is None:
        scheduling_conflict = []
    if cut_players is None:
        cut_players = []
    if keep_players is None:
        keep_players = []

    ref = pd.Timestamp(as_of) if as_of else pd.Timestamp.now()

    engine = get_engine()
    q = f"SELECT * FROM {model.standings_table} WHERE date >= '{model.data_cutoff}'"
    if model.played_filter:
        q += f' AND {model.played_filter}'
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
    N = int((ref - pd.Timestamp(model.data_cutoff)) / pd.Timedelta(weeks=1))

    df['skill_w'] = model.skill_decay          ** time_diff
    df['part_w']  = model.participation_decay  ** time_diff

    if attendance_start is None and model.season_shift_date is not None:
        shift_ts = pd.Timestamp(model.season_shift_date)
        df.loc[df['date'] < shift_ts, 'part_w'] *= model.season_shift_factor

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
            spec=spec,
        )

    # ── Participation calculation ────────────────────────────────────────────
    if attendance_start is not None:
        # Backtest mode: participation rate is derived exclusively from the
        # post-attendance_start window (skill history is unrestricted above).
        att_ref = pd.Timestamp(attendance_start)
        att_mask = df['date'] >= att_ref
        # As below, the denominator is built from the actual events so it uses the
        # same weights as the numerator. A backtest runs on the event date, which
        # makes the latest prior event one week old rather than zero.
        window_events = df.loc[att_mask].drop_duplicates('date')
        N_att = len(window_events)
        Z_part = window_events['part_w'].sum()
        num = df.loc[att_mask].groupby('username')['part_w'].sum()
        Z_user = (Z_part - num.index.to_series().map(conflict_deduction)
                            .fillna(0.0)).clip(lower=1e-6)
        p_participate = (num / Z_user).clip(lower=model.min_participation_rate, upper=1.0).rename('p_participate')
        # Ensure every player in the skill dataset has a p_participate entry
        p_participate = p_participate.reindex(
            df['username'].unique(), fill_value=model.min_participation_rate
        )
        n_adj = int((num.index.to_series().map(conflict_deduction).fillna(0.0) > 0).sum())
        print(f'  p_participate: EWMA over {N_att} events since {attendance_start} '
              f'(no season-shift); {n_adj:,} players had conflict-week deductions')
    else:
        # What a perfect-attendance player accumulates: one weight per event, with
        # the same recency and season-shift weights as the numerator. Summing the
        # actual events keeps the two aligned whenever this runs during the week.
        Z_part     = df.drop_duplicates('date')['part_w'].sum()
        num = df.groupby('username')['part_w'].sum()
        Z_user = (Z_part - num.index.to_series().map(conflict_deduction)
                            .fillna(0.0)).clip(lower=1e-6)
        p_participate = (num / Z_user).clip(lower=model.min_participation_rate, upper=1.0).rename('p_participate')
        n_adj = int((num.index.to_series().map(conflict_deduction).fillna(0.0) > 0).sum())
        print(f'  p_participate: {n_adj:,} players had conflict-week deductions applied to Z_part')

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


def load_model_predictions(market: str = TITLED_TUESDAY.slug) -> pd.DataFrame:
    """Load latest MC model predictions from DB."""
    return read_market_rows('latest_model_predictions', market)


def load_player_usernames() -> pd.DataFrame:
    """Load all player chess.com usernames form DB"""
    return pd.read_sql_query(
        'SELECT DISTINCT username FROM titled_tuesday_standings', get_engine()
    )


def load_backtest(model:str | None = None, since:str | None = None,
                  market:str = TITLED_TUESDAY.slug) -> pd.DataFrame:
    """Load joint backtest results (score vs perf-rating). model='score' or 'perf'."""
    q = 'SELECT * FROM backtest'
    wheres = [f"market = '{market}'"]
    if model:
        wheres.append(f"model = '{model}'")
    if since:
        wheres.append(f"tourn_date >= '{since}'")
    if wheres:
        q += ' WHERE ' + ' AND '.join(wheres)
    return pd.read_sql_query(q, get_engine(), parse_dates=['tourn_date'])
