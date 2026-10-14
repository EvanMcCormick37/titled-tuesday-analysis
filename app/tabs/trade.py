"""Trade tab — Phase 3 manual-trigger trading.

Three sub-sections (Place / Take / Cancel), each follows the same two-tap
pattern mandated by the plan:
    1. User fills a form and taps **Preview (dry run)**.
    2. The underlying trade function runs with `dry_run=True`; its full stdout
       and summary dict are captured and displayed verbatim.
    3. User reviews the plan, ticks an "I understand this is live" checkbox,
       and taps **Execute**. The *same* parameters are re-run with `dry_run=False`.

Every control is disabled when `pipeline_runs.state != 'completed'` — the plan's
stale-data guard — unless the user explicitly overrides via the escape hatch.
"""
from __future__ import annotations

import io
import traceback
from contextlib import redirect_stdout
from typing import Any, Callable

import streamlit as st

from app.state import current_status, invalidate_caches, next_tourn_date_str


# ── Shared helpers ───────────────────────────────────────────────────────────

def _client():
    """Lazy KalshiClient so env problems surface in the UI, not at import time."""
    from kalshi_core import KalshiClient
    return KalshiClient.from_env()


def _run_capturing(fn: Callable[..., dict], **kwargs) -> tuple[dict | None, str, str | None]:
    """Run `fn(**kwargs)`, capturing stdout. Returns (summary, printed_text, error).

    error is None on success; otherwise a formatted traceback string.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            summary = fn(**kwargs)
        return summary, buf.getvalue(), None
    except Exception:  # noqa: BLE001 — the UI surfaces every failure path
        return None, buf.getvalue(), traceback.format_exc()


def _render_plan_output(summary: dict | None, output: str, error: str | None) -> None:
    if error:
        st.error('Plan failed with an exception:')
        st.code(error, language='text')
    if output:
        st.code(output, language='text')
    if summary:
        st.caption('Returned summary:')
        st.json(summary)


# ── Preview/Execute scaffolding ──────────────────────────────────────────────

def _preview_execute_block(
    prefix: str,
    gate_ok: bool,
    tourn_date: str,
    fn: Callable[..., dict],
    params: dict[str, Any],
    execute_label: str,
) -> None:
    """Render the shared Preview → Confirm → Execute flow.

    `prefix` namespaces session_state so the three sub-sections don't collide.
    `fn` is the trading function (place_bids / take_trades / cancel_tt_bids).
    `params` is everything except `dry_run` and `client` — fn is called as
    `fn(client=..., dry_run=..., **params)`. We store params in session_state
    so Execute re-runs with the same ones the preview used.
    """
    plan_key   = f'{prefix}_plan'
    output_key = f'{prefix}_plan_output'
    error_key  = f'{prefix}_plan_error'
    params_key = f'{prefix}_plan_params'
    ack_key    = f'{prefix}_live_ack'

    # Preview (dry run)
    if st.button('Preview (dry run)', key=f'{prefix}_preview', disabled=not gate_ok, type='primary'):
        try:
            client = _client()
        except Exception as e:  # noqa: BLE001
            st.exception(e); return
        summary, output, err = _run_capturing(
            fn, client=client, tourn_date=tourn_date, dry_run=True, **params,
        )
        st.session_state[plan_key]   = summary
        st.session_state[output_key] = output
        st.session_state[error_key]  = err
        st.session_state[params_key] = params
        st.session_state[ack_key]    = False  # reset ack when preview re-runs

    # Render whatever's in session_state (if a preview has run)
    summary = st.session_state.get(plan_key)
    output  = st.session_state.get(output_key)
    error   = st.session_state.get(error_key)
    stored_params = st.session_state.get(params_key)

    if summary is None and output is None and error is None:
        return

    st.divider()
    st.markdown('#### Plan')
    _render_plan_output(summary, output, error)

    if error is not None:
        return

    # Guard the live execute path on parameter identity: if the user changed any
    # form input after previewing, require them to preview again.
    if stored_params != params:
        st.info('Form inputs changed since preview — re-run **Preview** before executing.')
        return

    st.divider()
    st.markdown('#### Execute (live)')
    st.warning('This will place real orders using live Kalshi API calls.')
    ack = st.checkbox('I confirm this will submit live orders to Kalshi',
                      key=ack_key)
    if st.button(execute_label, key=f'{prefix}_execute',
                 disabled=(not ack) or (not gate_ok), type='primary'):
        try:
            client = _client()
        except Exception as e:  # noqa: BLE001
            st.exception(e); return
        with st.spinner('Submitting live orders...'):
            summary, output, err = _run_capturing(
                fn, client=client, tourn_date=tourn_date, dry_run=False, **stored_params,
            )
        if err:
            st.error('Live execution raised an exception:')
            st.code(err, language='text')
            return
        if output:
            st.code(output, language='text')
        st.success('Live run complete.')
        st.json(summary)
        # Clear ack and plan so the user has to re-preview before another execute.
        for k in (plan_key, output_key, error_key, params_key):
            st.session_state.pop(k, None)
        st.session_state[ack_key] = False
        invalidate_caches()


# ── Sub-sections ─────────────────────────────────────────────────────────────

def _render_place(gate_ok: bool, tourn_date: str) -> None:
    st.markdown('### Place resting bids')
    st.caption(
        'Scans every TT market, computes fair = p_participate × P(top-N | plays), '
        'and places bids at max(fair / markup, fair − max_discount¢). Cancels '
        'existing orders if the new bid is lower.'
    )
    cols = st.columns(4)
    with cols[0]:
        count = st.number_input('count', 1, 10_000, 200, 1, key='place_count',
                                help='Contracts per bid')
    with cols[1]:
        markup = st.number_input('markup', 1.0, 10.0, 1.5, 0.05, key='place_markup',
                                 help='Bid = fair / markup. 1.5 → 2/3 of fair.')
    with cols[2]:
        max_discount = st.number_input('max_discount (¢)', 0.0, 50.0, 15.0, 1.0,
                                       key='place_max_disc',
                                       help='Hard cap on cents-off vs fair.')
    with cols[3]:
        side = st.selectbox('side', ('both', 'yes', 'no'), key='place_side')

    params = {
        'count': int(count),
        'markup': float(markup),
        'max_discount': float(max_discount),
        'side_filter': None if side == 'both' else side,
    }
    from src.trading import place_bids
    _preview_execute_block('place', gate_ok, tourn_date, place_bids, params,
                           execute_label='Place bids live')


def _render_take(gate_ok: bool, tourn_date: str) -> None:
    st.markdown('### Take trades')
    st.caption(
        'Fetches live asks across every TT market, filters for ROI > min_roi, '
        'and fires fill-or-kill takers. Levels with qty < min_qty are skipped '
        'as iceberg decoys.'
    )
    cols = st.columns(4)
    with cols[0]:
        min_roi = st.number_input('min_roi', 1.0, 5.0, 1.5, 0.05, key='take_min_roi')
    with cols[1]:
        min_qty = st.number_input('min_qty', 1, 1_000, 25, 5, key='take_min_qty')
    with cols[2]:
        max_qty = st.number_input('max_qty', 1, 10_000, 200, 10, key='take_max_qty')
    with cols[3]:
        best_per_event = st.toggle('best per event', value=True, key='take_bpe',
                                   help='Fire only the single highest-ROI trade per N-category.')
    budget = st.number_input('budget cap ($) — leave 0 for no cap', 0.0, 100_000.0, 0.0, 10.0,
                             key='take_budget')

    mode = st.radio(
        'Mode', ('One-shot', 'Loop'), horizontal=True, key='take_mode',
        help=('One-shot: Preview → Execute a single sweep. '
              'Loop: run sweeps continuously on an interval until stopped or budget exhausted.'),
    )

    base_params = {
        'min_roi':        float(min_roi),
        'min_qty':        int(min_qty),
        'max_qty':        int(max_qty),
        'best_per_event': bool(best_per_event),
    }
    budget_f = None if budget <= 0 else float(budget)

    if mode == 'One-shot':
        from src.trading import take_trades
        _preview_execute_block('take', gate_ok, tourn_date, take_trades,
                               {**base_params, 'budget_remaining': budget_f},
                               execute_label='Fire takers live')
    else:
        _render_take_loop(gate_ok, tourn_date, base_params, budget_f)


def _fmt_dt(dt) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else '—'


def _render_take_loop(gate_ok: bool, tourn_date: str, base_params: dict,
                      budget: float | None) -> None:
    """Loop controls + status, backed by `app.trade_loop` module-level thread."""
    from app import trade_loop

    cols = st.columns(2)
    with cols[0]:
        interval_minutes = st.number_input(
            'interval (min, between sweeps that found trades)',
            1.0, 240.0, 15.0, 1.0, key='take_loop_interval',
        )
    with cols[1]:
        fallback_wait_hours = st.number_input(
            'fallback wait (hours, when a sweep found nothing)',
            0.1, 12.0, 1.0, 0.1, key='take_loop_fallback',
        )

    state = trade_loop.get_state()
    running = state['running']

    st.markdown('#### Status')
    if running:
        c = st.columns(4)
        c[0].metric('State', 'running')
        c[1].metric('Iterations', state['iterations'])
        c[2].metric('Total spent ($)', f'{state["total_spent"]:.2f}')
        if budget is None:
            c[3].metric('Budget', 'none')
        else:
            c[3].metric('Budget remaining ($)',
                        f'{max(budget - state["total_spent"], 0.0):.2f}')
        st.caption(
            f'Started: {_fmt_dt(state["started_at"])} · '
            f'Last sweep: {_fmt_dt(state["last_sweep_at"])} · '
            f'Next sweep: {_fmt_dt(state["next_sweep_at"])}'
        )
    else:
        if state['stopped_at']:
            st.info(
                f'Last loop stopped at {_fmt_dt(state["stopped_at"])} '
                f'(reason: `{state["stop_reason"]}`, '
                f'{state["iterations"]} iteration(s), '
                f'${state["total_spent"]:.2f} spent).'
            )
        else:
            st.caption('No loop has run in this session.')

    st.markdown('#### Controls')
    if running:
        if st.button('Stop loop', key='take_loop_stop', type='primary'):
            ok, msg = trade_loop.stop()
            st.toast(msg, icon='✋' if ok else '⚠️')
            st.rerun()
        if st.button('Refresh status', key='take_loop_refresh'):
            st.rerun()
    else:
        st.warning(
            'The loop fires **live** taker orders every iteration — there is no '
            'per-sweep confirmation. Review the parameters above before starting.'
        )
        ack = st.checkbox(
            'I confirm this will submit live orders to Kalshi on an interval',
            key='take_loop_ack',
        )
        if st.button('Start loop', key='take_loop_start',
                     disabled=(not ack) or (not gate_ok), type='primary'):
            params = {
                **base_params,
                'tourn_date':          tourn_date,
                'interval_minutes':    float(interval_minutes),
                'fallback_wait_hours': float(fallback_wait_hours),
                'budget':              budget,
            }
            try:
                ok, msg = trade_loop.start(params)
            except Exception as e:  # noqa: BLE001
                st.exception(e); return
            st.toast(msg, icon='▶️' if ok else '⚠️')
            if ok:
                st.rerun()

    # History: compact, most-recent first.
    history = state['history']
    if history:
        st.markdown('#### Recent sweeps')
        for entry in reversed(history):
            icon = '✗' if entry['error'] else ('✓' if entry['edge_count'] else '○')
            label = (
                f'{icon} {_fmt_dt(entry["at"])} · '
                f'edges={entry["edge_count"]} · '
                f'exposure=${entry["exposure"]:.2f}'
            )
            with st.expander(label):
                if entry['error']:
                    st.error('Sweep raised an exception:')
                    st.code(entry['error'], language='text')
                if entry['stdout']:
                    st.code(entry['stdout'], language='text')
                if entry['summary']:
                    st.caption('Returned summary:')
                    st.json(entry['summary'])


def _render_fade(gate_ok: bool, tourn_date: str) -> None:
    st.markdown('### Fade NO bids (post-start expiry)')
    st.caption(
        'Resting NO bids priced at **(P(top-N | plays) × 100 − discount)¢**, '
        'expiring **N minutes after 11:00 ET tournament start**. Thesis: catch '
        'retail overpaying YES when a low-attendance star shows up on the roster. '
        'Uses the *conditional* probability — attendance is ignored because the '
        'fade only triggers once the player is already on the roster.'
    )
    cols = st.columns(3)
    with cols[0]:
        discount = st.number_input('discount (¢)', 1, 50, 15, 1, key='fade_discount',
                                   help='Cents off fair NO.')
    with cols[1]:
        count = st.number_input('count', 1, 10_000, 200, 1, key='fade_count',
                                help='Contracts per NO bid.')
    with cols[2]:
        minutes_after_start = st.number_input('expiry (min after 11:00 ET)',
                                              0, 180, 10, 1, key='fade_mas',
                                              help='Bids expire this many minutes after tourn start.')

    params = {
        'discount_cents':      int(discount),
        'count':               int(count),
        'minutes_after_start': int(minutes_after_start),
    }
    from src.trading import place_fade_no_bids
    _preview_execute_block('fade', gate_ok, tourn_date, place_fade_no_bids, params,
                           execute_label='Place fade NO bids live')


def _render_cancel(gate_ok: bool, tourn_date: str) -> None:
    st.markdown('### Cancel resting bids')
    st.caption(f'Cancels every resting order on TT markets dated **{tourn_date}**.')
    from src.trading import cancel_tt_bids
    _preview_execute_block('cancel', gate_ok, tourn_date, cancel_tt_bids, {},
                           execute_label='Cancel all TT bids live')


# ── Entry point ──────────────────────────────────────────────────────────────

def render() -> None:
    st.subheader('Trade')
    status = current_status()
    tourn_date = next_tourn_date_str()

    if not tourn_date:
        st.info('No pipeline run yet — trading needs predictions for an upcoming TT.')
        return

    gate_ok = status.state == 'completed'
    if not gate_ok:
        st.error(
            f'Pipeline state is `{status.state}` — trade controls are disabled '
            f'until it reaches `completed` (stale-data guard, plan §8).'
        )
        override = st.checkbox('I know the data may be stale — enable anyway')
        gate_ok = bool(override)

    st.caption(f'Target tournament: **{tourn_date}**')

    section = st.radio('Action',
                       ('Place bids', 'Fade NO bids', 'Take trades', 'Cancel bids'),
                       horizontal=True, label_visibility='collapsed')
    if section == 'Place bids':
        _render_place(gate_ok, tourn_date)
    elif section == 'Fade NO bids':
        _render_fade(gate_ok, tourn_date)
    elif section == 'Take trades':
        _render_take(gate_ok, tourn_date)
    else:
        _render_cancel(gate_ok, tourn_date)
