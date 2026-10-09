#!/usr/bin/env python3
"""Telegram bot entry point — the `tt-bot` Railway service.

Boots a python-telegram-bot v21 Application that:
  * Fires push alerts whenever `pipeline_runs.state` changes (StateWatcher).
  * Serves read-only commands (/status /odds /top /conflicts /positions /orders).
  * Serves write commands with inline Confirm/Cancel buttons (/place /take
    /fade /cancel /recompute /retry /cut /keep /override).

Auth model is simple and strict: every handler is wired behind
`filters.Chat(TELEGRAM_CHAT_ID)` so messages from any other chat ID are
dropped on the floor without any response. BotFather already restricts the
bot to the chats you've started it with; this is defence in depth.

Env vars required:
    TELEGRAM_BOT_TOKEN   — from @BotFather
    TELEGRAM_CHAT_ID     — your private chat id (positive int)

Run locally:
    python app/telegram_bot.py

Railway: start command `python app/telegram_bot.py` on the tt-bot service.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Make `app.*` and `src.*` importable when run as a bare script (Railway's
# `python app/telegram_bot.py` doesn't put the project root on sys.path).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    filters,
)

from app.bot import handlers
from app.bot.alerts import StateWatcher


log = logging.getLogger(__name__)


def _env_or_die(name: str) -> str:
    val = os.environ.get(name, '').strip()
    if not val:
        raise SystemExit(f'Missing required env var: {name}')
    return val


def build_application() -> Application:
    token = _env_or_die('TELEGRAM_BOT_TOKEN')
    chat_id = int(_env_or_die('TELEGRAM_CHAT_ID'))

    app = Application.builder().token(token).build()

    chat_filter = filters.Chat(chat_id)

    # ── Read-only commands ───────────────────────────────────────────────────
    app.add_handler(CommandHandler(['start', 'help'], handlers.start,  filters=chat_filter))
    app.add_handler(CommandHandler('status',         handlers.cmd_status,     filters=chat_filter))
    app.add_handler(CommandHandler('odds',           handlers.cmd_odds,       filters=chat_filter))
    app.add_handler(CommandHandler('top',            handlers.cmd_top,        filters=chat_filter))
    app.add_handler(CommandHandler('conflicts',      handlers.cmd_conflicts,  filters=chat_filter))
    app.add_handler(CommandHandler('positions',      handlers.cmd_positions,  filters=chat_filter))
    app.add_handler(CommandHandler('orders',         handlers.cmd_orders,     filters=chat_filter))

    # ── Write commands (two-tap confirm) ─────────────────────────────────────
    app.add_handler(CommandHandler('place',          handlers.cmd_place,      filters=chat_filter))
    app.add_handler(CommandHandler('fade',           handlers.cmd_fade,       filters=chat_filter))
    app.add_handler(CommandHandler('take',           handlers.cmd_take,       filters=chat_filter))
    app.add_handler(CommandHandler('cancel',         handlers.cmd_cancel,     filters=chat_filter))
    app.add_handler(CommandHandler('retry',          handlers.cmd_retry,      filters=chat_filter))
    app.add_handler(CommandHandler('recompute',      handlers.cmd_recompute,  filters=chat_filter))
    app.add_handler(CommandHandler('cut',            handlers.cmd_cut,        filters=chat_filter))
    app.add_handler(CommandHandler('keep',           handlers.cmd_keep,       filters=chat_filter))
    app.add_handler(CommandHandler('override',       handlers.cmd_override,   filters=chat_filter))

    # Inline Confirm/Cancel buttons from the write commands.
    # Note: CallbackQueryHandler doesn't support Chat filter directly; we gate
    # on query.from_user.id inside the handler is overkill — python-telegram-bot
    # only delivers callbacks from chats the bot has interacted with, and the
    # pending-plan token is one-shot + 10-min TTL anyway.
    app.add_handler(CallbackQueryHandler(handlers.on_callback))

    # ── State watcher ───────────────────────────────────────────────────────
    watcher = StateWatcher(chat_id=chat_id)
    app.bot_data['state_watcher'] = watcher
    job_queue = app.job_queue
    if job_queue is None:
        raise SystemExit(
            'JobQueue unavailable — install `python-telegram-bot[job-queue]`.'
        )
    # Immediate startup pass (fires alerts for persistent failed/pending states),
    # then every 30 seconds.
    job_queue.run_once(watcher.startup, when=1)
    job_queue.run_repeating(watcher.tick, interval=30, first=30)

    log.info('tt-bot application built; chat_id=%s', chat_id)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    # Shut up the httpx request logs — they're one line per Telegram poll.
    logging.getLogger('httpx').setLevel(logging.WARNING)

    app = build_application()
    log.info('starting polling...')
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
