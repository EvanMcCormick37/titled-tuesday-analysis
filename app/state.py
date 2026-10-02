"""Cross-tab data helpers: pipeline-run status, predictions loader.

Centralises DB reads that multiple tabs need, so caching + state-shape stays
consistent. Caches are short (60s) so the dashboard reflects a run_adjusted
trigger within one refresh after it finishes.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import NamedTuple

import pandas as pd
import streamlit as st

from src.db import get_conn, get_engine


class PipelineStatus(NamedTuple):
    id:            int | None
    tourn_date:    str | None       # the Tuesday whose standings were scraped
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


@st.cache_data(ttl=15, show_spinner=False)
def current_status() -> PipelineStatus:
    """Return the pipeline_runs row for the most-recent Tuesday.

    `state='absent'` indicates no row exists yet (first-ever run hasn't fired).
    """
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
    return PipelineStatus(*(str(v) if v is not None and not isinstance(v, int) else v for v in row))


@st.cache_data(ttl=30, show_spinner=False)
def load_predictions(adjusted: bool = True) -> pd.DataFrame:
    """Load the current adjusted or raw predictions DataFrame.

    Columns include: username, p_participate, P_top{N}_given_play, P_top{N},
    ip_adv_top{N}, tourn_date. Indexed by username.
    """
    table = 'latest_model_predictions' if adjusted else 'latest_model_predictions_raw'
    df = pd.read_sql_query(f'SELECT * FROM {table}', get_engine())
    if df.empty:
        return df
    df = df.set_index('username')
    for n in (1, 3, 5, 8, 10):
        col_ip = f'P_top{n}_given_play'
        if col_ip not in df.columns:
            continue
        df[f'P_top{n}']       = df['p_participate'] * df[col_ip]
        df[f'ip_adv_top{n}']  = df[col_ip] / df[f'P_top{n}'].replace(0, pd.NA)
    return df


@st.cache_data(ttl=120, show_spinner=False)
def username_to_player_map() -> dict[str, str]:
    """Map chess.com username → display player_name (via player_information)."""
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT username, player_name FROM player_information '
            'WHERE player_name IS NOT NULL'
        ).fetchall()
    return {u: p for u, p in rows}


@st.cache_data(ttl=120, show_spinner=False)
def all_player_names() -> list[str]:
    """Distinct player_name strings — for the Adjustments tab autocomplete."""
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT DISTINCT player_name FROM player_information '
            'WHERE player_name IS NOT NULL '
            'ORDER BY player_name'
        ).fetchall()
    return [r[0] for r in rows]


def next_tourn_date_str() -> str:
    """Date of the TT we're predicting (one Tuesday after `current_status().tourn_date`)."""
    s = current_status()
    if s.tourn_date is None:
        return ''
    return (pd.Timestamp(s.tourn_date) + pd.Timedelta(weeks=1)).date().isoformat()


def invalidate_caches() -> None:
    """Call after any write (adjustment, trigger_adjusted, retry) so the UI sees fresh data."""
    current_status.clear()
    load_predictions.clear()
    all_player_names.clear()
    username_to_player_map.clear()
