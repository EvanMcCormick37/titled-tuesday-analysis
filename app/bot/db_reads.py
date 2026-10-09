"""Non-cached DB readers mirroring app.state, usable outside a Streamlit context.

The dashboard's app/state.py decorates its loaders with @st.cache_data, which
emits warnings and gates on a ScriptRunContext when called from a plain Python
process (like the bot). These are the same queries without the cache layer.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import NamedTuple

import pandas as pd

from src.db import get_conn, get_engine


class PipelineStatus(NamedTuple):
    """Shared pipeline-run row shape. Used by the dashboard and the bot alike."""
    id:            int | None
    tourn_date:    str | None
    state:         str              # 'running' | 'adjustments_pending' | 'completed' | 'failed' | 'absent'
    current_step:  str | None
    failed_step:   str | None
    error_kind:    str | None
    error_message: str | None
    started_at:    str | None
    completed_at:  str | None


def _most_recent_tuesday(d: date | None = None) -> date:
    d = d or date.today()
    return d - timedelta(days=(d.weekday() - 1) % 7)


def current_status() -> PipelineStatus:
    tourn_date = _most_recent_tuesday().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            'SELECT id, tourn_date, state, current_step, failed_step, '
            'error_kind, error_message, started_at, completed_at '
            'FROM pipeline_runs WHERE tourn_date = ?',
            (tourn_date,),
        ).fetchone()
    if row is None:
        return PipelineStatus(None, tourn_date, 'absent',
                              None, None, None, None, None, None)
    return PipelineStatus(*(
        str(v) if v is not None and not isinstance(v, int) else v for v in row
    ))


def load_predictions(adjusted: bool = True) -> pd.DataFrame:
    table = 'latest_model_predictions' if adjusted else 'latest_model_predictions_raw'
    df = pd.read_sql_query(f'SELECT * FROM {table}', get_engine())
    if df.empty:
        return df
    df = df.set_index('username')
    for n in (1, 3, 5, 8, 10):
        col_ip = f'P_top{n}_given_play'
        if col_ip not in df.columns:
            continue
        df[f'P_top{n}'] = df['p_participate'] * df[col_ip]
    return df


def username_to_player_map() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT username, player_name FROM player_information '
            'WHERE player_name IS NOT NULL'
        ).fetchall()
    return {u: p for u, p in rows}


def player_name_to_username_map() -> dict[str, str]:
    """Default-flagged username per display name. Falls back to any username."""
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT player_name, username, is_default FROM player_information '
            'WHERE player_name IS NOT NULL'
        ).fetchall()
    out: dict[str, str] = {}
    for name, username, is_default in rows:
        if name not in out or is_default:
            out[name] = username
    return out


def next_tourn_date_str() -> str:
    s = current_status()
    if s.tourn_date is None:
        return ''
    return (pd.Timestamp(s.tourn_date) + pd.Timedelta(weeks=1)).date().isoformat()


def adjustments_for(tourn_date: str) -> list[tuple[str, str, float | None]]:
    """Return (player_name, adjustment_type, value) rows for tourn_date."""
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT player_name, adjustment_type, value FROM manual_adjustments '
            'WHERE tourn_date = ? ORDER BY created_at ASC',
            (tourn_date,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def insert_adjustment(tourn_date: str, player_name: str, adjustment_type: str,
                      value: float | None, created_by: str = 'telegram') -> None:
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO manual_adjustments '
            '(tourn_date, player_name, adjustment_type, value, created_by) '
            'VALUES (?, ?, ?, ?, ?)',
            (tourn_date, player_name, adjustment_type, value, created_by),
        )
        conn.commit()
