"""Jobs tab — paginated log viewer of every job_runs row.

Click-to-expand to see full summary + log_tail traceback.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.db import get_conn


def _load_jobs(limit: int) -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT id, pipeline_run_id, job_name, status, started_at, ended_at, '
            'summary, log_tail FROM job_runs ORDER BY id DESC LIMIT ?',
            (limit,),
        ).fetchall()
    return pd.DataFrame(rows, columns=['id', 'pipeline_run_id', 'job_name', 'status',
                                       'started_at', 'ended_at', 'summary', 'log_tail'])


def _render_row(row: pd.Series) -> None:
    status_icon = {'success': '✓', 'failed': '✗', 'running': '…'}.get(row['status'], '?')
    label = (f'{status_icon} `{row["job_name"]}` · {row["status"]} · '
             f'pipeline_run_id={row["pipeline_run_id"]} · '
             f'started {row["started_at"]}')
    with st.expander(label):
        c = st.columns(3)
        c[0].caption(f'Started: {row["started_at"]}')
        c[1].caption(f'Ended: {row["ended_at"] or "—"}')
        c[2].caption(f'Job ID: {row["id"]}')

        if row.get('summary'):
            try:
                parsed = json.loads(row['summary']) if isinstance(row['summary'], str) else row['summary']
                st.json(parsed)
            except (ValueError, TypeError):
                st.text(str(row['summary']))

        if row.get('log_tail'):
            st.text_area('Error log tail', row['log_tail'], height=240,
                         key=f'logtail_{row["id"]}', disabled=True)


def render() -> None:
    st.subheader('Jobs')
    cols = st.columns([1, 2, 1])
    with cols[0]:
        limit = st.selectbox('Limit', (20, 50, 100, 200), index=1)
    with cols[1]:
        job_filter = st.multiselect(
            'Filter by job',
            ('scrape_tt', 'update_broadcasts', 'run_raw', 'run_adjusted',
             'place_bids', 'take_trades', 'cancel_bids'),
            default=[],
        )
    with cols[2]:
        status_filter = st.multiselect('Status', ('success', 'failed', 'running'), default=[])

    df = _load_jobs(limit)
    if job_filter:
        df = df[df['job_name'].isin(job_filter)]
    if status_filter:
        df = df[df['status'].isin(status_filter)]

    if df.empty:
        st.caption('No job_runs rows match the filter.')
        return

    st.caption(f'{len(df):,} job(s)')
    for _, row in df.iterrows():
        _render_row(row)
