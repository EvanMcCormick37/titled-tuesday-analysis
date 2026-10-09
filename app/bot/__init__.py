"""Telegram bot package for Titled Tuesday.

See `app/telegram_bot.py` for the entry point. This package holds the pieces:
    formatters.py — pure text builders (pipeline status, odds, positions)
    alerts.py     — StateWatcher: polls pipeline_runs, pushes alerts on diffs
    handlers.py   — command + callback handlers (read-only and write)
"""
