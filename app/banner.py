"""Persistent pipeline-status banner, rendered at the top of every tab.

Colour-coded by state. For every failed/pending state, embeds the specific
action the user needs to take (retry, paste URL, save & recompute) so the
banner itself is the primary control surface for recovery.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from app.state import current_status, invalidate_caches, next_tourn_date_str


def _humanize_delta(iso_ts: str | None) -> str:
    if not iso_ts:
        return 'unknown'
    try:
        ts = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return iso_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    s = int(delta.total_seconds())
    if s < 60:   return f'{s}s ago'
    if s < 3600: return f'{s // 60}m ago'
    if s < 86400: return f'{s // 3600}h ago'
    return f'{s // 86400}d ago'


def render() -> None:
    """Render the status strip for the current week's pipeline_runs row."""
    s = current_status()
    predicting = next_tourn_date_str()

    if s.state == 'completed':
        st.success(
            f"**Completed** · scraped TT **{s.tourn_date}** · "
            f"predictions ready for **{predicting}** · "
            f"finished {_humanize_delta(s.completed_at)}"
        )
        return

    if s.state == 'absent':
        _render_absent(s)
        return

    if s.state == 'running':
        st.info(
            f"**Running** · scraped TT **{s.tourn_date}** · "
            f"current step: `{s.current_step or '?'}`"
        )
        if st.button('Refresh', key='banner_refresh_running'):
            invalidate_caches(); st.rerun()
        return

    if s.state == 'adjustments_pending':
        _render_pending(s, predicting)
        return

    if s.state == 'failed':
        _render_failed(s)
        return

    st.warning(f'Unknown pipeline state: {s.state!r}')


def _render_absent(s) -> None:
    st.info(
        f"**No pipeline run yet** for TT **{s.tourn_date}**. "
        'The weekly cron will fire automatically on Tuesday 15:00 ET. '
        'If you want to run it manually right now, use the Pipeline tab.'
    )


def _render_pending(s, predicting: str) -> None:
    st.warning(
        f"**Adjustments pending** · raw predictions ready for **{predicting}** · "
        f'edit the Attendance Adjustments tab and tap **Save & recompute**, '
        f'or go directly to the Pipeline tab.'
    )


def _render_failed(s) -> None:
    if s.error_kind == 'listing_missing_slug':
        st.error(
            f"**Scrape failed** · chess.com listing has no new TT slug for **{s.tourn_date}** yet. "
            f'Find the tournament URL on chess.com and paste it below:'
        )
        with st.form('banner_slug_url', clear_on_submit=False):
            url = st.text_input(
                'TT tournament URL',
                placeholder='https://www.chess.com/tournament/live/titled-tuesday-blitz-...',
                label_visibility='collapsed',
            )
            submitted = st.form_submit_button('Scrape with this URL', type='primary')
        if submitted:
            if not url.strip():
                st.warning('Paste a URL first.')
            else:
                _run_pipeline(slug_override=url.strip())
        return

    kind = s.error_kind or 'unknown'
    msg = s.error_message or '(no message)'
    st.error(
        f"**Failed** at step `{s.failed_step}` ({kind}): {msg[:220]}"
        + ('…' if len(msg) > 220 else '')
    )
    if st.button('Retry pipeline', key='banner_retry', type='primary'):
        _run_pipeline()


def _run_pipeline(slug_override: str | None = None) -> None:
    """Trigger weekly_pipeline.run_pipeline() synchronously with a spinner."""
    from scripts.weekly_pipeline import run_pipeline

    msg = 'Running pipeline...'
    if slug_override:
        msg = 'Scraping with your URL + running downstream steps...'
    with st.spinner(msg):
        try:
            run_pipeline(slug_override=slug_override)
        except Exception as e:  # noqa: BLE001 — surface anything to the UI
            st.exception(e)
            return
    invalidate_caches()
    st.rerun()
