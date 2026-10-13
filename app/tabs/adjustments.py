"""Attendance Adjustments tab — bulk editable form.

Mirrors the top-of-notebook config cell in notebooks/command-center.ipynb:
one text block per adjustment type (list / dict / scalar), submitted together.
Each save REPLACES all manual_adjustments rows for the current tourn_date,
so what's in the textboxes IS the full state.
"""
from __future__ import annotations

import ast

import streamlit as st

from app.state import invalidate_caches, next_tourn_date_str
from src.db import get_conn
from src.pipeline import load_adjustments


# ── DB write ──────────────────────────────────────────────────────────────────

def _replace_all(
    tourn_date: str,
    cut: list[str],
    keep: list[str],
    overrides: dict[str, float],
    nudges: dict[str, float],
    global_nudge: float,
) -> None:
    """Replace every manual_adjustments row for `tourn_date` with the new state."""
    with get_conn() as conn:
        conn.execute('DELETE FROM manual_adjustments WHERE tourn_date = ?', (tourn_date,))

        def _ins(player: str, kind: str, value: float | None) -> None:
            conn.execute(
                'INSERT INTO manual_adjustments '
                '(tourn_date, player_name, adjustment_type, value, created_by) '
                'VALUES (?, ?, ?, ?, ?)',
                (tourn_date, player, kind, value, 'dashboard'),
            )

        for name in cut:
            _ins(name, 'cut', None)
        for name in keep:
            _ins(name, 'keep', None)
        for name, val in overrides.items():
            _ins(name, 'override', float(val))
        for name, val in nudges.items():
            _ins(name, 'nudge', float(val))
        if global_nudge:
            _ins('', 'global_nudge', float(global_nudge))


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_list(text: str, label: str) -> list[str]:
    s = text.strip()
    if not s:
        return []
    try:
        val = ast.literal_eval(s)
    except (SyntaxError, ValueError) as e:
        raise ValueError(f'{label}: could not parse ({e})')
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ValueError(f'{label}: expected a list of strings, got {type(val).__name__}')
    return val


def _parse_num_dict(text: str, label: str) -> dict[str, float]:
    s = text.strip()
    if not s:
        return {}
    try:
        val = ast.literal_eval(s)
    except (SyntaxError, ValueError) as e:
        raise ValueError(f'{label}: could not parse ({e})')
    if not isinstance(val, dict):
        raise ValueError(f'{label}: expected a dict, got {type(val).__name__}')
    for k, v in val.items():
        if not isinstance(k, str) or not isinstance(v, (int, float)):
            raise ValueError(f'{label}: expected dict[str, number] — bad entry {k!r}: {v!r}')
    return {k: float(v) for k, v in val.items()}


# ── Pretty-printers ───────────────────────────────────────────────────────────

def _fmt_list(xs: list[str]) -> str:
    if not xs:
        return '[]'
    return '[\n    ' + ',\n    '.join(repr(x) for x in xs) + ',\n]'


def _fmt_num_dict(d: dict[str, float], fmt: str = '{:+.3f}') -> str:
    if not d:
        return '{}'
    lines = [f'    {k!r}: {fmt.format(v)}' for k, v in d.items()]
    return '{\n' + ',\n'.join(lines) + ',\n}'


# ── UI ────────────────────────────────────────────────────────────────────────

def _render_bulk_form(tourn_date: str) -> None:
    current = load_adjustments(tourn_date)

    with st.form('adj_bulk_form'):
        st.caption(
            'Edit each block as a Python literal (list / dict / number). '
            'Submitting **replaces all** adjustments for this tourn_date — '
            'what you see here is the full state.'
        )

        cut_txt = st.text_area(
            'CUT_PLAYERS — list[str]  (p_participate → 0)',
            value=_fmt_list(current['cut_players']),
            height=120,
        )
        keep_txt = st.text_area(
            'KEEP_PLAYERS — list[str]  (p_participate → 1)',
            value=_fmt_list(current['keep_players']),
            height=120,
        )
        over_txt = st.text_area(
            'P_OVERRIDES — dict[str, float 0..1]  (explicit p_participate)',
            value=_fmt_num_dict(current['p_participate_overrides']),
            height=160,
        )
        nudges_txt = st.text_area(
            'P_NUDGES — dict[str, float]  (per-player log-odds shift; +raises)',
            value=_fmt_num_dict(current['p_nudges']),
            height=160,
        )
        global_nudge = st.number_input(
            'GLOBAL_NUDGE — log-odds shift applied to every player',
            value=float(current['global_nudge']), step=0.1, format='%.2f',
        )

        col1, col2 = st.columns(2)
        save      = col1.form_submit_button('Save', width='stretch')
        save_run  = col2.form_submit_button('Save & recompute', type='primary', width='stretch')

    if not (save or save_run):
        return

    try:
        cut     = _parse_list(cut_txt,     'CUT_PLAYERS')
        keep    = _parse_list(keep_txt,    'KEEP_PLAYERS')
        over    = _parse_num_dict(over_txt,   'P_OVERRIDES')
        nudges  = _parse_num_dict(nudges_txt, 'P_NUDGES')
    except ValueError as e:
        st.error(str(e))
        return

    _replace_all(tourn_date, cut, keep, over, nudges, float(global_nudge))
    invalidate_caches()

    if save_run:
        from scripts.trigger_adjusted import trigger_adjusted
        with st.spinner('Running adjusted MC (100,000 sims)…'):
            try:
                summary = trigger_adjusted(tourn_date)
            except Exception as e:  # noqa: BLE001
                st.exception(e)
                return
        invalidate_caches()
        st.success(
            f'Saved + recomputed — {summary["pool_size"]:,} players, '
            f'{summary["overrides"]} overrides, {summary["nudges"]} nudges applied.'
        )
    else:
        st.success(
            f'Saved: {len(cut)} cut, {len(keep)} keep, '
            f'{len(over)} override, {len(nudges)} nudge, '
            f'global_nudge={global_nudge:+.2f}.'
        )
    st.rerun()


def render() -> None:
    st.subheader('Attendance adjustments')
    predicting = next_tourn_date_str()
    if not predicting:
        st.info('No pipeline run yet — adjustments require the raw predictions to exist first.')
        return

    st.markdown(f'Editing adjustments for **{predicting}**.')
    _render_bulk_form(predicting)
