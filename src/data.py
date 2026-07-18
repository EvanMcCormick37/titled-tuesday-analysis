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

_USERNAME_TO_PLAYER: dict | None = None
_PLAYER_TO_USERNAME: dict | None = None


def get_username_mappings() -> tuple[dict, dict]:
    """Return (username→player, player→username) dicts, loaded lazily from DB."""
    global _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME
    if _USERNAME_TO_PLAYER is None:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            'SELECT username, player_name FROM player_information'
        ).fetchall()
        conn.close()
        _USERNAME_TO_PLAYER = {u: p for u, p in rows}
        _PLAYER_TO_USERNAME = {p: u for u, p in rows}
    return _USERNAME_TO_PLAYER, _PLAYER_TO_USERNAME


# ── Main data loader ──────────────────────────────────────────────────────────

def load_and_prepare(cut_players=None, keep_players=None,
                     attendance_model='decay', target_date=None):
    """
    Load standings from DB and compute per-player MC inputs.

    attendance_model options
    ------------------------
    'decay'         – recency-weighted EWMA (default)
    'gradient_boost'– gradient boosted trees (src/attendance.py)
    'random_forest' – random forest          (src/attendance.py)
    'logistic'      – logistic regression    (src/attendance.py)

    target_date : pd.Timestamp used by ML models to compute features.
        Defaults to one week after the last date in the data.
        Ignored for attendance_model='decay'.
    """
    if cut_players is None:
        cut_players = []
    if keep_players is None:
        keep_players = []

    _, PLAYER_TO_USERNAME = get_username_mappings()
    cut_users  = [PLAYER_TO_USERNAME[p] for p in cut_players if p in PLAYER_TO_USERNAME]
    keep_users = [PLAYER_TO_USERNAME[p] for p in keep_players if p in PLAYER_TO_USERNAME]

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"SELECT * FROM titled_tuesday_standings WHERE date >= '{DATA_CUTOFF}'",
        conn, parse_dates=['date'],
    )
    conn.close()

    df = (
        df.sort_values('rank')
          .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
    )

    time_diff = (pd.Timestamp.now() - pd.to_datetime(df['date'])) // pd.Timedelta(weeks=1)
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

    # Optionally replace EWMA with ML model predictions
    if attendance_model != 'decay':
        try:
            from .attendance import (
                load_data as _am_load, get_weekly_slots,
                load_models as _am_load_models, get_p_participate_for_mc,
            )
            if target_date is None:
                last_date   = df['date'].max()
                target_date = last_date + pd.Timedelta(weeks=1)

            _df    = _am_load()
            _slots = get_weekly_slots(_df)
            _mdls  = _am_load_models()
            ml_p   = get_p_participate_for_mc(
                _df, _slots, _mdls, pd.Timestamp(target_date),
                model_name=attendance_model,
            )
            n_updated = ml_p.index.isin(p_participate.index).sum()
            p_participate.update(ml_p)
            print(f'  p_participate: {attendance_model} model '
                  f'({n_updated:,} players, {len(p_participate) - n_updated:,} EWMA fallback)')
        except Exception as e:
            print(f'  p_participate: falling back to EWMA ({e})')
    else:
        print('  p_participate: decay-weighted EWMA with isotonic floor (p < 0.05)')

    p_participate[p_participate.index.isin(cut_users)]  = 0.0
    p_participate[p_participate.index.isin(keep_users)] = 1.0

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
