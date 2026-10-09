"""StateWatcher — polls pipeline_runs for transitions, pushes alerts.

Design
------
On startup, we scan every pipeline_runs row and record its current state in an
in-memory dict `self._seen = {pr_id: state}`. For any row whose current state
is `failed` or `adjustments_pending`, we *also* fire a startup alert — this
catches state transitions the bot missed while down.

Every tick (default 30s), we re-scan, diff against `self._seen`, and fire a
transition alert for each (pr_id, state) that's new. The dict is small (one
row per Tuesday) so there's no eviction logic — it grows one entry per week.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.constants import ParseMode

from app.bot import db_reads, formatters
from src.db import get_conn

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

log = logging.getLogger(__name__)


class StateWatcher:
    """Hooks into python-telegram-bot's JobQueue to emit alerts on state changes."""

    STARTUP_ALERT_STATES = {'failed', 'adjustments_pending'}

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self._seen: dict[int, str] = {}
        self._initialized = False

    # ── Internals ────────────────────────────────────────────────────────────

    def _scan(self) -> list[dict]:
        """Return a list of {id, tourn_date, state, current_step, failed_step,
        error_kind, error_message} for all recent pipeline_runs."""
        with get_conn() as conn:
            rows = conn.execute(
                'SELECT id, tourn_date, state, current_step, failed_step, '
                '       error_kind, error_message, started_at, completed_at '
                'FROM pipeline_runs ORDER BY tourn_date DESC LIMIT 8'
            ).fetchall()
        from app.bot.db_reads import PipelineStatus
        return [PipelineStatus(*r) for r in rows]

    def _diff(self, rows) -> list[tuple[object, str | None]]:
        """Return list of (status_namedtuple, prev_state_or_None_if_new)."""
        changes = []
        for s in rows:
            prev = self._seen.get(s.id)
            if prev != s.state:
                changes.append((s, prev))
        return changes

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def startup(self, context: 'ContextTypes.DEFAULT_TYPE') -> None:
        """One-shot: initialise self._seen and fire alerts for persistent sticky states."""
        rows = self._scan()
        startup_alerts = []
        for s in rows:
            if s.state in self.STARTUP_ALERT_STATES:
                startup_alerts.append((s, None))
            self._seen[s.id] = s.state
        self._initialized = True
        for s, prev in startup_alerts:
            await self._send_alert(context, s, prev, startup=True)

    async def tick(self, context: 'ContextTypes.DEFAULT_TYPE') -> None:
        if not self._initialized:
            await self.startup(context)
            return
        rows = self._scan()
        for s, prev in self._diff(rows):
            self._seen[s.id] = s.state
            await self._send_alert(context, s, prev, startup=False)

    # ── Sending ──────────────────────────────────────────────────────────────

    async def _send_alert(self, context, status, prev_state, *, startup: bool) -> None:
        text = formatters.alert_pipeline_transition(status, prev_state)
        if startup:
            text = '(startup) ' + text
        try:
            await context.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # noqa: BLE001 — logged, don't crash the loop
            log.exception('Failed to send alert: %s', e)


def mark_adjusted_complete_as_seen(watcher: StateWatcher) -> None:
    """Call after /recompute or dashboard Save&recompute flips a row to completed.

    Prevents a duplicate transition alert when the bot's next tick picks up the
    'completed' state we just set. (The command handler already acknowledged
    the user explicitly.)
    """
    for s in watcher._scan():  # noqa: SLF001
        if s.state == 'completed':
            watcher._seen[s.id] = 'completed'  # noqa: SLF001
