"""Attendance Adjustments tab — the critical weekly input surface.

Replaces the top cell of notebooks/command-center.ipynb. Writes directly to
the manual_adjustments table; the "Save & recompute" button fires
scripts.trigger_adjusted.trigger_adjusted() synchronously.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.state import (
    all_player_names,
    invalidate_caches,
    next_tourn_date_str,
)
from src.db import get_conn

_ADJ_TYPES = ('cut', 'keep', 'override', 'nudge', 'global_nudge')
_NEEDS_VALUE = {'override', 'nudge', 'global_nudge'}
_NEEDS_PLAYER = {'cut', 'keep', 'override', 'nudge'}


def _load_adjustments(tourn_date: str) -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT id, player_name, adjustment_type, value, note, created_at, created_by '
            'FROM manual_adjustments WHERE tourn_date = ? ORDER BY created_at DESC',
            (tourn_date,),
        ).fetchall()
    return pd.DataFrame(rows, columns=['id', 'player_name', 'adjustment_type', 'value',
                                       'note', 'created_at', 'created_by'])


def _insert_adjustment(tourn_date: str, player: str, kind: str,
                       value: float | None, note: str) -> None:
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO manual_adjustments '
            '(tourn_date, player_name, adjustment_type, value, note, created_by) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (tourn_date, player, kind, value, note.strip() or None, 'dashboard'),
        )


def _delete_adjustment(row_id: int) -> None:
    with get_conn() as conn:
        conn.execute('DELETE FROM manual_adjustments WHERE id = ?', (row_id,))


def _render_add_form(tourn_date: str, players: list[str]) -> None:
    st.markdown('### Add adjustment')
    with st.form('adj_form', clear_on_submit=True):
        kind = st.selectbox('Type', _ADJ_TYPES, index=0, help=_TYPE_HELP)
        player = ''
        if kind in _NEEDS_PLAYER:
            player = st.selectbox('Player', options=players, index=None,
                                  placeholder='Start typing a player name…')
        value = None
        if kind in _NEEDS_VALUE:
            if kind == 'nudge' or kind == 'global_nudge':
                value = st.number_input(
                    'Log-odds shift', value=0.0, step=0.1, format='%.2f',
                    help='Positive = raise attendance. Interpreted as a log-odds additive shift.',
                )
            else:  # override
                value = st.number_input(
                    'p_participate', min_value=0.0, max_value=1.0, value=0.5, step=0.05,
                    format='%.2f', help='Explicit probability override, between 0 and 1.',
                )
        note = st.text_input('Note (optional)', placeholder='e.g. "WR tournament clash"')
        submitted = st.form_submit_button('Add', type='primary')

    if not submitted:
        return
    if kind in _NEEDS_PLAYER and not player:
        st.warning('Pick a player first.')
        return
    if kind == 'global_nudge':
        player = ''  # sentinel
    _insert_adjustment(tourn_date, player, kind, value, note)
    invalidate_caches()
    st.success(f'Added {kind}' + (f' for {player}' if player else ''))
    st.rerun()


def _render_current_table(tourn_date: str) -> None:
    df = _load_adjustments(tourn_date)
    st.markdown(f'### Current adjustments for **{tourn_date}**')
    if df.empty:
        st.caption('_No adjustments yet._')
        return

    # One row per adjustment, with a delete button.
    header = st.columns([2, 2, 1.5, 3, 1])
    header[0].markdown('**Player**')
    header[1].markdown('**Type**')
    header[2].markdown('**Value**')
    header[3].markdown('**Note**')
    header[4].markdown('**Delete**')
    for _, row in df.iterrows():
        c = st.columns([2, 2, 1.5, 3, 1])
        c[0].write(row['player_name'] or '_(global)_')
        c[1].write(f"`{row['adjustment_type']}`")
        c[2].write('—' if row['value'] is None else f"{row['value']:+.3f}")
        c[3].write(row['note'] or '')
        if c[4].button('✕', key=f'del_{row["id"]}', help='Delete this adjustment'):
            _delete_adjustment(int(row['id']))
            invalidate_caches()
            st.rerun()


def _render_recompute_section(tourn_date: str) -> None:
    st.markdown('### Save & recompute')
    st.caption(
        'Writes `manual_adjustments` rows (above) through `pipeline.load_adjustments()` '
        'into `run_adjusted()`. Takes ~60s to run 100k simulations. The Odds tab '
        'and pipeline banner refresh when it finishes.'
    )
    if st.button('Save & recompute', type='primary', width='stretch'):
        from scripts.trigger_adjusted import trigger_adjusted
        with st.spinner('Running adjusted MC (100,000 sims)…'):
            try:
                summary = trigger_adjusted(tourn_date)
            except Exception as e:  # noqa: BLE001
                st.exception(e)
                return
        invalidate_caches()
        st.success(f'OK — {summary["pool_size"]:,} players, {summary["overrides"]} overrides, '
                   f'{summary["nudges"]} nudges applied.')
        st.rerun()


_TYPE_HELP = (
    '• **cut** — force p_participate = 0.\n'
    '• **keep** — force p_participate = 1.\n'
    '• **override** — set p_participate to an explicit value.\n'
    '• **nudge** — add a log-odds shift to a single player.\n'
    '• **global_nudge** — add a log-odds shift to every player.'
)


def render() -> None:
    st.subheader('Attendance adjustments')
    predicting = next_tourn_date_str()
    if not predicting:
        st.info('No pipeline run yet — adjustments require the raw predictions to exist first.')
        return

    _render_current_table(predicting)
    st.divider()
    _render_add_form(predicting, all_player_names())
    st.divider()
    _render_recompute_section(predicting)
