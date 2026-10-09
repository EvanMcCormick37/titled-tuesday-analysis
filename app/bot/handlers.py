"""Telegram command + callback handlers.

Each public command here is registered in `app/telegram_bot.py` behind a
chat-ID auth filter — so no handler needs its own auth check.

Design: write commands show a dry-run plan with inline Confirm / Cancel
buttons. On Confirm, the same function re-runs with `dry_run=False` (or for
adjustments, the actual DB insert happens).  Pending plans are stashed in
`_PENDING` keyed by a 10-char token; they expire after 10 minutes.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import secrets
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.bot import db_reads, formatters

log = logging.getLogger(__name__)

PLAN_TTL_SECONDS = 600  # 10 minutes


# ── Pending-plan cache ───────────────────────────────────────────────────────

@dataclass
class PendingPlan:
    action: str
    description: str                # user-facing label, e.g. 'place_bids'
    executor: Callable[[], Any]     # closure that performs the LIVE action
    created_at: float = field(default_factory=time.time)


_PENDING: dict[str, PendingPlan] = {}


def _gc_pending() -> None:
    now = time.time()
    stale = [t for t, p in _PENDING.items() if now - p.created_at > PLAN_TTL_SECONDS]
    for t in stale:
        _PENDING.pop(t, None)


def _stash(plan: PendingPlan) -> str:
    _gc_pending()
    token = secrets.token_urlsafe(6)
    _PENDING[token] = plan
    return token


def _confirm_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('Confirm', callback_data=f'ok:{token}'),
        InlineKeyboardButton('Cancel',  callback_data=f'no:{token}'),
    ]])


# ── Shared helpers ───────────────────────────────────────────────────────────

def _lazy_client():
    """KalshiClient.from_env() — raises if credentials missing."""
    from kalshi_core import KalshiClient
    client = KalshiClient.from_env()
    if getattr(client, '_private_key', None) is None:
        raise RuntimeError('Kalshi credentials not loaded (set KALSHI_API_KEY_ID + '
                           'KALSHI_PRIVATE_KEY_B64).')
    return client


async def _run_in_thread(fn: Callable[..., Any], **kwargs) -> tuple[Any, str, str | None]:
    """Run blocking `fn` in a thread, capturing stdout. Returns (result, output, error)."""
    def _call():
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                r = fn(**kwargs)
            return r, buf.getvalue(), None
        except Exception:
            return None, buf.getvalue(), traceback.format_exc()
    return await asyncio.to_thread(_call)


async def _reply_long(update: Update, text: str, **kwargs) -> None:
    """Send message, splitting into chunks if it exceeds Telegram's 4096 limit."""
    chunk_size = 4000
    if len(text) <= chunk_size:
        await update.effective_message.reply_text(text, **kwargs)
        return
    for i in range(0, len(text), chunk_size):
        await update.effective_message.reply_text(text[i:i + chunk_size], **kwargs)


# ── Help / start ─────────────────────────────────────────────────────────────

HELP_TEXT = (
    '<b>Titled Tuesday bot</b>\n'
    '\n'
    '<b>Read</b>\n'
    '/status — pipeline state for this Tuesday\n'
    '/odds &lt;player&gt; — probability + fair prices\n'
    '/top &lt;N&gt; — leaderboard by P_top&lt;N&gt;\n'
    '/conflicts — current adjustments\n'
    '/positions — open Kalshi positions\n'
    '/orders — open Kalshi orders\n'
    '\n'
    '<b>Write (all two-tap)</b>\n'
    '/place — place resting bids\n'
    '/fade — place fade NO bids\n'
    '/take — fire taker orders\n'
    '/cancel — cancel all TT bids\n'
    '/recompute — re-run adjusted MC\n'
    '/retry [&lt;url&gt;] — resume halted pipeline\n'
    '/cut &lt;player&gt; · /keep &lt;player&gt; · /override &lt;player&gt; &lt;prob&gt;\n'
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# ── Read-only commands ──────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = db_reads.current_status()
    await update.effective_message.reply_text(
        formatters.pipeline_status(s), parse_mode=ParseMode.HTML,
    )


async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.effective_message.reply_text(
            'Usage: <code>/odds &lt;player name or username&gt;</code>',
            parse_mode=ParseMode.HTML)
        return
    needle = ' '.join(args).strip().lower()
    df = db_reads.load_predictions(adjusted=True)
    if df.empty:
        await update.effective_message.reply_text('No predictions in DB.')
        return
    uname_to_player = db_reads.username_to_player_map()
    # match by username first, then by player_name (case-insensitive substring)
    hit = None
    if needle in df.index.str.lower():
        hit = df.loc[df.index.str.lower() == needle].iloc[0]
    else:
        matches = [u for u, p in uname_to_player.items()
                   if needle in p.lower() or needle == u.lower()]
        matches = [u for u in matches if u in df.index]
        if matches:
            hit = df.loc[matches[0]]
    if hit is None:
        await update.effective_message.reply_text(
            f'No match for <code>{needle}</code>.', parse_mode=ParseMode.HTML)
        return
    label = uname_to_player.get(hit.name, hit.name)
    await update.effective_message.reply_text(
        formatters.odds_for_player(label, hit), parse_mode=ParseMode.HTML)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    try:
        n = int(args[0]) if args else 5
    except ValueError:
        await update.effective_message.reply_text(
            'Usage: <code>/top &lt;N&gt;</code>  (N in 1,3,5,8,10)',
            parse_mode=ParseMode.HTML)
        return
    if n not in (1, 3, 5, 8, 10):
        await update.effective_message.reply_text(f'N must be one of 1,3,5,8,10; got {n}.')
        return
    df = db_reads.load_predictions(adjusted=True)
    if df.empty:
        await update.effective_message.reply_text('No predictions in DB.')
        return
    await update.effective_message.reply_text(
        formatters.top_n_leaderboard(df, n), parse_mode=ParseMode.HTML)


async def cmd_conflicts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tourn_date = db_reads.next_tourn_date_str()
    if not tourn_date:
        await update.effective_message.reply_text('No upcoming TT date resolved.')
        return
    rows = db_reads.adjustments_for(tourn_date)
    await update.effective_message.reply_text(
        formatters.adjustments_list(rows), parse_mode=ParseMode.HTML)


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        client = _lazy_client()
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f'Kalshi: {e}')
        return
    from src.kalshi_tt import get_tt_positions_df
    df = await asyncio.to_thread(get_tt_positions_df, client)
    await update.effective_message.reply_text(
        formatters.positions_df(df), parse_mode=ParseMode.HTML)


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        client = _lazy_client()
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f'Kalshi: {e}')
        return
    tourn = db_reads.next_tourn_date_str()
    orders = await asyncio.to_thread(client.get_open_orders)
    # filter to TT markets for this upcoming date
    from kalshi_core import _to_kalshi_date
    kdate = _to_kalshi_date(tourn) if tourn else ''
    tt_orders = [o for o in orders if kdate and kdate in o.get('ticker', '')]
    await update.effective_message.reply_text(
        formatters.orders_list(tt_orders), parse_mode=ParseMode.HTML)


# ── Write-command scaffolding ────────────────────────────────────────────────

async def _preview_and_stash(
    update: Update,
    action: str,
    dry_run_fn: Callable[[], Any],
    live_executor: Callable[[], Any],
) -> None:
    """Run `dry_run_fn()` in a thread, format the plan, stash `live_executor` behind a token."""
    result, output, err = await _run_in_thread(dry_run_fn)
    if err:
        await update.effective_message.reply_text(
            f'<b>FAIL dry-run failed</b>\n<pre>{err[-1500:]}</pre>',
            parse_mode=ParseMode.HTML)
        return
    text = formatters.plan_preview(action, result or {}, output)
    plan = PendingPlan(action=action, description=action, executor=live_executor)
    token = _stash(plan)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=_confirm_markup(token))


async def _gated(update: Update, require_completed: bool = True) -> str | None:
    """Return the upcoming TT date iff the pipeline is in `completed`, else send
    an error and return None. Set `require_completed=False` for commands that
    don't need pipeline freshness (e.g. /retry)."""
    tourn = db_reads.next_tourn_date_str()
    if not tourn:
        await update.effective_message.reply_text('No upcoming TT resolved.')
        return None
    if not require_completed:
        return tourn
    s = db_reads.current_status()
    if s.state != 'completed':
        await update.effective_message.reply_text(
            f'Pipeline state is <code>{s.state}</code> — '
            f'trades blocked until <code>completed</code>.',
            parse_mode=ParseMode.HTML)
        return None
    return tourn


# ── Trade commands ───────────────────────────────────────────────────────────

async def cmd_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tourn = await _gated(update)
    if not tourn:
        return
    try:
        client = _lazy_client()
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f'Kalshi: {e}')
        return
    from src.trading import place_bids
    kwargs = dict(client=client, tourn_date=tourn, count=200, markup=1.5, max_discount=15.0)
    await _preview_and_stash(
        update, 'place_bids',
        dry_run_fn=lambda: place_bids(dry_run=True,  **kwargs),
        live_executor=lambda: place_bids(dry_run=False, **kwargs),
    )


async def cmd_fade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tourn = await _gated(update)
    if not tourn:
        return
    try:
        client = _lazy_client()
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f'Kalshi: {e}')
        return
    from src.trading import place_fade_no_bids
    kwargs = dict(client=client, tourn_date=tourn, discount_cents=15,
                  count=200, minutes_after_start=10)
    await _preview_and_stash(
        update, 'place_fade_no_bids',
        dry_run_fn=lambda: place_fade_no_bids(dry_run=True,  **kwargs),
        live_executor=lambda: place_fade_no_bids(dry_run=False, **kwargs),
    )


async def cmd_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tourn = await _gated(update)
    if not tourn:
        return
    try:
        client = _lazy_client()
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f'Kalshi: {e}')
        return
    from src.trading import take_trades
    kwargs = dict(client=client, tourn_date=tourn, min_roi=1.5, min_qty=25,
                  max_qty=200, best_per_event=True)
    await _preview_and_stash(
        update, 'take_trades',
        dry_run_fn=lambda: take_trades(dry_run=True,  **kwargs),
        live_executor=lambda: take_trades(dry_run=False, **kwargs),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tourn = await _gated(update, require_completed=False)  # OK to cancel anytime
    if not tourn:
        return
    try:
        client = _lazy_client()
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f'Kalshi: {e}')
        return
    from src.trading import cancel_tt_bids
    await _preview_and_stash(
        update, 'cancel_tt_bids',
        dry_run_fn=lambda: cancel_tt_bids(client=client, tourn_date=tourn, dry_run=True),
        live_executor=lambda: cancel_tt_bids(client=client, tourn_date=tourn, dry_run=False),
    )


# ── Pipeline control ────────────────────────────────────────────────────────

async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry a halted pipeline. Optional URL arg → slug override for scrape_tt."""
    url = context.args[0] if context.args else None
    s = db_reads.current_status()
    if s.state not in ('failed',):
        await update.effective_message.reply_text(
            f'Pipeline is <code>{s.state}</code> — nothing to retry.',
            parse_mode=ParseMode.HTML)
        return
    from scripts.weekly_pipeline import run_pipeline
    kwargs = {'slug_override': url} if url else {}
    executor = lambda: run_pipeline(**kwargs)
    # No separate dry-run for pipeline retry — show what will happen in text, then confirm.
    label = f'retry (step {s.failed_step}' + (f', url={url}' if url else '') + ')'
    plan = PendingPlan(action=label, description=label, executor=executor)
    token = _stash(plan)
    await update.effective_message.reply_text(
        f'<b>Retry pipeline</b>\nResume from <code>{s.failed_step}</code>'
        + (f' with slug <code>{url}</code>' if url else '') + '?',
        parse_mode=ParseMode.HTML, reply_markup=_confirm_markup(token))


async def cmd_recompute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run run_adjusted() with current manual_adjustments."""
    tourn = db_reads.next_tourn_date_str()
    if not tourn:
        await update.effective_message.reply_text('No upcoming TT resolved.')
        return
    from scripts.trigger_adjusted import trigger_adjusted
    plan = PendingPlan(
        action='recompute',
        description='run_adjusted',
        executor=lambda: trigger_adjusted(tourn),
    )
    token = _stash(plan)
    rows = db_reads.adjustments_for(tourn)
    detail = f'{len(rows)} adjustment(s) staged for <code>{tourn}</code>.'
    await update.effective_message.reply_text(
        f'<b>Recompute</b>\n{detail}\nRun run_adjusted now?',
        parse_mode=ParseMode.HTML, reply_markup=_confirm_markup(token))


# ── Adjustment commands ─────────────────────────────────────────────────────

async def _confirm_adjustment(update: Update, kind: str, player: str,
                              value: float | None) -> None:
    tourn = db_reads.next_tourn_date_str()
    if not tourn:
        await update.effective_message.reply_text('No upcoming TT resolved.')
        return
    plan = PendingPlan(
        action=f'{kind} {player}',
        description=f'manual_adjustment ({kind})',
        executor=lambda: db_reads.insert_adjustment(
            tourn, player, kind, value, created_by='telegram'),
    )
    token = _stash(plan)
    vtxt = f' = <code>{value}</code>' if value is not None else ''
    await update.effective_message.reply_text(
        f'<b>Adjustment preview</b>\n'
        f'  type: <code>{kind}</code>\n'
        f'  player: <code>{player}</code>{vtxt}\n'
        f'  tourn_date: <code>{tourn}</code>',
        parse_mode=ParseMode.HTML, reply_markup=_confirm_markup(token))


async def cmd_cut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text('Usage: <code>/cut &lt;player name&gt;</code>',
                                                  parse_mode=ParseMode.HTML)
        return
    player = ' '.join(context.args)
    await _confirm_adjustment(update, 'cut', player, None)


async def cmd_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text('Usage: <code>/keep &lt;player name&gt;</code>',
                                                  parse_mode=ParseMode.HTML)
        return
    player = ' '.join(context.args)
    await _confirm_adjustment(update, 'keep', player, None)


async def cmd_override(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            'Usage: <code>/override &lt;player name&gt; &lt;prob 0..1&gt;</code>',
            parse_mode=ParseMode.HTML)
        return
    try:
        prob = float(args[-1])
    except ValueError:
        await update.effective_message.reply_text('Prob must be a number in 0..1.')
        return
    if not 0.0 <= prob <= 1.0:
        await update.effective_message.reply_text('Prob out of range (expected 0..1).')
        return
    player = ' '.join(args[:-1])
    await _confirm_adjustment(update, 'override', player, prob)


# ── Callback (inline button) dispatcher ──────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()  # ack the button so Telegram clears the loader
    data = q.data or ''
    if ':' not in data:
        await q.edit_message_text('malformed callback')
        return
    verb, token = data.split(':', 1)
    plan = _PENDING.pop(token, None)
    if plan is None:
        await q.edit_message_text(
            '<i>(plan expired or already handled — re-run the command)</i>',
            parse_mode=ParseMode.HTML)
        return

    if verb == 'no':
        await q.edit_message_text(f'<i>Cancelled: {plan.action}</i>',
                                  parse_mode=ParseMode.HTML)
        return
    if verb != 'ok':
        await q.edit_message_text('unknown verb')
        return

    # Execute live in a thread.
    await q.edit_message_text(f'<i>Executing: {plan.action}...</i>',
                              parse_mode=ParseMode.HTML)
    result, output, err = await _run_in_thread(plan.executor)
    if err:
        await q.message.reply_text(
            f'<b>FAIL {plan.action} failed</b>\n<pre>{err[-1500:]}</pre>',
            parse_mode=ParseMode.HTML)
        return
    await q.message.reply_text(
        formatters.live_result(plan.action, result if isinstance(result, dict) else {}, output or ''),
        parse_mode=ParseMode.HTML)
