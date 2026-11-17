"""Append-only JSONL journal of every inbound message and outbound action.

One file per UTC day under $MM_JOURNAL_DIR (default data/mm_journal). On
Railway the container disk is ephemeral, so mount a volume there to keep
journals across deploys. Writes are best-effort: a full disk must never stop
quoting.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class Journal:
    def __init__(self, directory: str | Path | None = None, enabled: bool = True):
        self.dir = Path(directory or os.environ.get('MM_JOURNAL_DIR')
                        or Path(__file__).resolve().parents[2] / 'data' / 'mm_journal')
        self.enabled = enabled
        self._day = None
        self._fh = None

    def _file(self):
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if day != self._day:
            if self._fh:
                self._fh.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.dir / f'{day}.jsonl', 'a', encoding='utf-8')
            self._day = day
        return self._fh

    def write(self, kind: str, payload: dict) -> None:
        if not self.enabled:
            return
        try:
            fh = self._file()
            fh.write(json.dumps({'t': time.time(), 'm': time.monotonic(), 'k': kind, 'p': payload},
                                default=str) + '\n')
        except Exception:  # noqa: BLE001
            log.warning('journal write failed', exc_info=True)

    def flush(self) -> None:
        try:
            if self._fh:
                self._fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
