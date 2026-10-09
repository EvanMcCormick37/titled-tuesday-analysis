"""Pure text formatters — no side effects, no DB reads here.

Caller passes in rows / dicts / DataFrames; these return Telegram-ready strings
(HTML parse_mode, keep it under Telegram's 4096 character limit — truncate with
a tail marker when a table is long).
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

TELEGRAM_HARD_LIMIT = 4096
_SOFT_LIMIT = 3900  # leaves room for a trailing "... (truncated)" line


def _trim(text: str) -> str:
    if len(text) <= TELEGRAM_HARD_LIMIT:
        return text
    return text[:_SOFT_LIMIT] + '\n... (truncated)'


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return '--'
    return f'{x * 100:.1f}%'


# ── Pipeline status ─────────────────────────────────────────────────────────

def pipeline_status(status) -> str:
    """`status` is an app.state.PipelineStatus NamedTuple."""
    if status.state == 'absent':
        return (f'<b>Pipeline — {status.tourn_date}</b>\n'
                f'No run yet for this Tuesday.')

    icon = {
        'running':             '[running]',
        'adjustments_pending': '[pending]',
        'completed':           '[OK]',
        'failed':              '[FAIL]',
    }.get(status.state, '[?]')

    lines = [f'<b>Pipeline — {status.tourn_date}</b>  {icon} <code>{status.state}</code>']
    if status.current_step:
        lines.append(f'current step: <code>{status.current_step}</code>')
    if status.state == 'failed':
        lines.append(f'failed at: <code>{status.failed_step}</code>  ({status.error_kind})')
        if status.error_message:
            lines.append(f'<pre>{status.error_message[:400]}</pre>')
        if status.error_kind == 'listing_missing_slug':
            lines.append('Reply <code>/retry &lt;url&gt;</code> with the TT url.')
        else:
            lines.append('Reply <code>/retry</code> to try again.')
    elif status.state == 'adjustments_pending':
        lines.append('Raw predictions ready. Review adjustments then <code>/recompute</code>.')
    elif status.state == 'completed' and status.completed_at:
        lines.append(f'completed at <code>{status.completed_at}</code>')
    return _trim('\n'.join(lines))


def alert_pipeline_transition(status, prev_state: str | None) -> str:
    """One-line transition alert prefixed with `[pipeline]`."""
    base = f'[pipeline] {status.tourn_date}'
    if status.state == 'completed':
        return f'{base} OK fully adjusted + predictions saved.'
    if status.state == 'adjustments_pending':
        return (f'{base} raw predictions ready — review adjustments then '
                f'/recompute (or do it from the dashboard).')
    if status.state == 'failed':
        tail = {
            'listing_missing_slug': 'reply /retry <url>.',
            'http_error':           'reply /retry to resume.',
            'computation_error':    'see /status for the traceback.',
        }.get(status.error_kind or '', 'reply /retry to resume.')
        return f'{base} FAIL at {status.failed_step} ({status.error_kind}) — {tail}'
    if status.state == 'running':
        return f'{base} running: {status.current_step}'
    return f'{base} {status.state}'


def alert_adjusted_complete(summary: Mapping) -> str:
    """Alert after /recompute (or dashboard Save & recompute) finishes."""
    n = summary.get('cut_players', 0) + summary.get('keep_players', 0) + \
        summary.get('overrides', 0) + summary.get('nudges', 0)
    gnudge = summary.get('global_nudge', 0.0)
    extra = f', global_nudge {gnudge:+.2f}' if gnudge else ''
    return (f'[run_adjusted] OK {n} adjustment(s) applied'
            f'{extra}. Target: {summary.get("tourn_date_predicted")}')


# ── Odds ─────────────────────────────────────────────────────────────────────

def odds_for_player(player: str, row) -> str:
    """`row` is a pandas Series from load_predictions, indexed by column name."""
    if row is None:
        return f'No prediction for <b>{player}</b>.'
    p_part = float(row.get('p_participate', 0.0))
    lines = [f'<b>{player}</b>  (<code>{row.name}</code>)',
             f'p_participate: <code>{_fmt_pct(p_part)}</code>']
    for n in (1, 3, 5, 8):
        given = row.get(f'P_top{n}_given_play')
        uncond = row.get(f'P_top{n}')
        if given is None:
            continue
        fair_cents = int(round(float(uncond) * 100)) if uncond is not None else 0
        lines.append(
            f'top-{n}: given {_fmt_pct(float(given))} / '
            f'uncond {_fmt_pct(float(uncond))}  '
            f'(fair YES {fair_cents}¢ / NO {100 - fair_cents}¢)'
        )
    return _trim('\n'.join(lines))


def top_n_leaderboard(df, n: int, k: int = 10) -> str:
    col = f'P_top{n}'
    if col not in df.columns:
        return f'No column <code>{col}</code> in predictions.'
    top = df.sort_values(col, ascending=False).head(k)
    lines = [f'<b>Top {k} by P_top{n}</b>']
    for i, (uname, r) in enumerate(top.iterrows(), 1):
        lines.append(
            f'{i:2d}. <code>{uname}</code>  '
            f'{_fmt_pct(float(r[col]))} '
            f'(given {_fmt_pct(float(r[f"P_top{n}_given_play"]))} · '
            f'play {_fmt_pct(float(r["p_participate"]))})'
        )
    return _trim('\n'.join(lines))


def adjustments_list(rows: Sequence) -> str:
    if not rows:
        return 'No adjustments for the upcoming TT.'
    lines = ['<b>Current adjustments</b>']
    for player, kind, value in rows:
        vtxt = f' = {value}' if value is not None else ''
        lines.append(f'• {kind:9s} <code>{player}</code>{vtxt}')
    return _trim('\n'.join(lines))


# ── Trading ──────────────────────────────────────────────────────────────────

def positions_df(df) -> str:
    if df is None or df.empty:
        return 'No open TT positions.'
    lines = ['<b>Open TT positions</b>']
    for _, r in df.iterrows():
        side = 'YES' if int(r['position']) > 0 else 'NO'
        qty = abs(int(r['position']))
        price = float(r.get('avg_price_incl_fees', r['avg_price']))
        lines.append(
            f'• <code>{r["marketTitle"]}</code>  {side} x {qty} @ {price*100:.1f}¢'
        )
    try:
        cost = float(df['cost'].abs().sum())
        fees = float(df['fees'].sum())
        total = float(df['cost_incl_fees'].abs().sum())
        lines.append(f'\n<i>Exposure {cost:,.2f} + fees {fees:,.2f} = {total:,.2f}</i>')
    except Exception:
        pass
    return _trim('\n'.join(lines))


def orders_list(orders: Iterable[Mapping]) -> str:
    orders = list(orders)
    if not orders:
        return 'No open TT orders.'
    lines = ['<b>Open TT orders</b>']
    for o in orders:
        price = o.get('yes_price') if o.get('side') == 'yes' else o.get('no_price')
        lines.append(
            f'• <code>{o["ticker"]}</code>  '
            f'{o["side"].upper()} x {o["remaining_count"]} @ {price}¢'
        )
    return _trim('\n'.join(lines))


def plan_preview(action: str, summary: Mapping, printed: str = '') -> str:
    """Confirmation preview before firing live orders.

    Caller gives us the summary dict from the dry-run and (optionally) captured
    stdout — we assemble the Telegram-friendly message.
    """
    lines = [f'<b>Plan — {action}</b>']
    if printed.strip():
        tail = printed.strip().splitlines()[-20:]
        lines.append('<pre>' + '\n'.join(tail) + '</pre>')
    if summary:
        key_lines = []
        for k, v in summary.items():
            if isinstance(v, (list, dict)):
                v = f'{len(v)} item(s)'
            key_lines.append(f'  {k}: {v}')
        lines.append('<b>Summary</b>')
        lines.append('<pre>' + '\n'.join(key_lines) + '</pre>')
    return _trim('\n'.join(lines))


def live_result(action: str, summary: Mapping, printed: str = '') -> str:
    lines = [f'<b>OK {action} live run complete</b>']
    if printed.strip():
        tail = printed.strip().splitlines()[-15:]
        lines.append('<pre>' + '\n'.join(tail) + '</pre>')
    if summary:
        key_lines = [f'  {k}: {v if not isinstance(v, (list, dict)) else f"{len(v)} item(s)"}'
                     for k, v in summary.items()]
        lines.append('<pre>' + '\n'.join(key_lines) + '</pre>')
    return _trim('\n'.join(lines))
