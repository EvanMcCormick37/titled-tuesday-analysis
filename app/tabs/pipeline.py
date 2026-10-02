"""Pipeline tab — detailed view of the current `pipeline_runs` row + retry controls.

Duplicates some banner functionality but with a per-step timeline so you can
see which steps succeeded in the current week before the failure.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from app.state import current_status, invalidate_caches
from src.db import get_conn

_STEPS = ('scrape_tt', 'update_broadcasts', 'run_raw')


def _step_jobs(pipeline_run_id: int) -> dict[str, dict]:
    """Return {step_name: latest job_runs row dict} for this pipeline run."""
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT job_name, status, started_at, ended_at, summary, log_tail '
            'FROM job_runs WHERE pipeline_run_id = ? ORDER BY id DESC',
            (pipeline_run_id,),
        ).fetchall()
    out: dict[str, dict] = {}
    for job_name, status, started, ended, summary, log_tail in rows:
        if job_name in out:
            continue  # keep only the latest row per step
        out[job_name] = {
            'status': status, 'started_at': str(started) if started else None,
            'ended_at': str(ended) if ended else None,
            'summary': summary, 'log_tail': log_tail,
        }
    return out


def _render_timeline(status, jobs: dict[str, dict]) -> None:
    for step in _STEPS:
        job = jobs.get(step)
        if job is None:
            if status.state in ('running', 'absent') and status.current_step == step:
                st.info(f'`{step}` — running')
            else:
                st.caption(f'`{step}` — not started')
            continue

        icon = {'success': '✓', 'failed': '✗', 'running': '…'}.get(job['status'], '?')
        header = f'{icon} `{step}` — {job["status"]}'
        with st.expander(header, expanded=(job['status'] == 'failed')):
            cols = st.columns(2)
            cols[0].caption(f'Started: {job["started_at"] or "—"}')
            cols[1].caption(f'Ended: {job["ended_at"] or "—"}')
            if job.get('summary'):
                try:
                    parsed = json.loads(job['summary']) if isinstance(job['summary'], str) else job['summary']
                    st.json(parsed)
                except (ValueError, TypeError):
                    st.text(job['summary'])
            if job.get('log_tail'):
                st.text_area('Error traceback (tail)', job['log_tail'], height=200,
                             key=f'tb_{step}', disabled=True)


def _render_actions(status) -> None:
    if status.state == 'completed':
        return

    if status.state == 'absent':
        st.caption('No `pipeline_runs` row for this week yet.')
        if st.button('Run pipeline now', type='primary'):
            _trigger()
        return

    if status.state == 'adjustments_pending':
        st.caption('Pipeline needs attendance adjustments → see the Attendance tab, or recompute with no changes:')
        if st.button('Recompute now (no adjustments)', type='primary'):
            _trigger_adjusted()
        return

    if status.state == 'running':
        st.caption('Pipeline is actively running — refresh to see progress.')
        if st.button('Refresh'):
            invalidate_caches(); st.rerun()
        return

    if status.state == 'failed':
        if status.error_kind == 'listing_missing_slug':
            with st.form('pipeline_slug_url'):
                url = st.text_input('TT tournament URL',
                                    placeholder='https://www.chess.com/tournament/live/titled-tuesday-blitz-...')
                if st.form_submit_button('Scrape with this URL', type='primary') and url.strip():
                    _trigger(slug_override=url.strip())
            return
        if st.button('Retry pipeline', type='primary'):
            _trigger()


def _trigger(slug_override: str | None = None) -> None:
    from scripts.weekly_pipeline import run_pipeline
    with st.spinner('Running pipeline…'):
        try:
            run_pipeline(slug_override=slug_override)
        except Exception as e:  # noqa: BLE001
            st.exception(e); return
    invalidate_caches(); st.rerun()


def _trigger_adjusted() -> None:
    from scripts.trigger_adjusted import trigger_adjusted
    with st.spinner('Running adjusted MC…'):
        try:
            trigger_adjusted()
        except Exception as e:  # noqa: BLE001
            st.exception(e); return
    invalidate_caches(); st.rerun()


def render() -> None:
    st.subheader('Pipeline')
    status = current_status()

    cols = st.columns([2, 1])
    cols[0].write(f'**Current state:** `{status.state}` for scraped TT **{status.tourn_date}**')
    if cols[1].button('Refresh', key='pipe_refresh'):
        invalidate_caches(); st.rerun()

    if status.state == 'failed' and status.error_message:
        st.error(status.error_message[:400] + ('…' if len(status.error_message) > 400 else ''))

    _render_actions(status)

    st.divider()
    st.markdown('### Steps')
    if status.id is None:
        st.caption('No `pipeline_runs` row yet.')
        return
    _render_timeline(status, _step_jobs(status.id))

    st.divider()
    st.markdown('### Recent pipeline runs')
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT tourn_date, state, failed_step, error_kind, started_at, completed_at '
            'FROM pipeline_runs ORDER BY tourn_date DESC LIMIT 10'
        ).fetchall()
    df = pd.DataFrame(rows, columns=['tourn_date', 'state', 'failed_step',
                                     'error_kind', 'started_at', 'completed_at'])
    st.dataframe(df, width='stretch', hide_index=True)
