"""Background take-trades loop for the dashboard.

Owns a single daemon thread that mirrors `scripts/take_trades.py`'s loop:
each iteration fires `take_trades(dry_run=False)` and sleeps `interval`
minutes when trades are found or `fallback_wait` hours when the sweep is dry.
The loop exits when the user stops it, the cumulative exposure hits the
budget, or three consecutive sweeps raise exceptions.

The module is a singleton — at most one loop can run per `tt-app` process.
State is kept in module-level variables guarded by a lock; the UI reads a
snapshot via `get_state()`. The thread is a daemon so it dies with the
Streamlit process (acceptable: a Railway redeploy restarts everything
anyway, and the loop is a convenience over the CLI script, not a system
of record).
"""
from __future__ import annotations

import io
import threading
import traceback
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshi_core import KalshiClient
from src.trading import take_trades

_MAX_CONSECUTIVE_ERRORS = 3
_HISTORY_MAXLEN = 20


@dataclass
class _LoopState:
    running: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_sweep_at: datetime | None = None
    next_sweep_at: datetime | None = None
    iterations: int = 0
    total_spent: float = 0.0
    stop_reason: str | None = None
    last_error: str | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=_HISTORY_MAXLEN))


_state = _LoopState()
_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _snapshot() -> dict[str, Any]:
    """Thread-safe copy of state suitable for the UI."""
    with _lock:
        return {
            'running':       _state.running,
            'params':        dict(_state.params),
            'started_at':    _state.started_at,
            'stopped_at':    _state.stopped_at,
            'last_sweep_at': _state.last_sweep_at,
            'next_sweep_at': _state.next_sweep_at,
            'iterations':    _state.iterations,
            'total_spent':   _state.total_spent,
            'stop_reason':   _state.stop_reason,
            'last_error':    _state.last_error,
            'history':       list(_state.history),
        }


def get_state() -> dict[str, Any]:
    return _snapshot()


def is_running() -> bool:
    with _lock:
        return _state.running


def _run_loop(params: dict[str, Any]) -> None:
    """Thread body — one `tt-app`-process worth of looping."""
    interval_s      = float(params['interval_minutes']) * 60.0
    fallback_s      = float(params['fallback_wait_hours']) * 3600.0
    budget          = params['budget']           # float or None (no cap)
    tourn_date      = params['tourn_date']
    take_kwargs     = {k: params[k] for k in
                       ('min_roi', 'min_qty', 'max_qty', 'best_per_event')}

    client = KalshiClient.from_env()
    consecutive_errors = 0
    stop_reason: str | None = None

    while True:
        if _stop_event.is_set():
            stop_reason = 'manual-stop'
            break

        budget_remaining = (None if budget is None
                            else max(budget - _state.total_spent, 0.0))

        sweep_started = datetime.now()
        buf = io.StringIO()
        summary: dict | None = None
        err: str | None = None
        try:
            with redirect_stdout(buf):
                summary = take_trades(
                    client,
                    tourn_date=tourn_date,
                    dry_run=False,
                    budget_remaining=budget_remaining,
                    **take_kwargs,
                )
            consecutive_errors = 0
        except Exception:  # noqa: BLE001 — one bad sweep must not kill the loop
            err = traceback.format_exc()
            consecutive_errors += 1

        exposure    = float(summary.get('exposure', 0.0)) if summary else 0.0
        edge_count  = int(summary.get('edge_count', 0)) if summary else 0
        budget_hit  = bool(summary.get('budget_exceeded', False)) if summary else False

        # Trades succeeded → trade-interval wait; dry sweep → fallback wait.
        wait_s = interval_s if edge_count > 0 else fallback_s
        next_sweep = datetime.now().timestamp() + wait_s

        with _lock:
            _state.iterations    += 1
            _state.last_sweep_at  = sweep_started
            _state.next_sweep_at  = datetime.fromtimestamp(next_sweep)
            _state.total_spent   += exposure
            _state.last_error     = err
            _state.history.append({
                'at':         sweep_started,
                'summary':    summary,
                'stdout':     buf.getvalue(),
                'error':      err,
                'exposure':   exposure,
                'edge_count': edge_count,
            })

        if budget_hit:
            stop_reason = 'budget-exhausted'
            break
        if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            stop_reason = f'{_MAX_CONSECUTIVE_ERRORS}-consecutive-errors'
            break

        # Interruptible sleep — wake instantly on stop().
        if _stop_event.wait(wait_s):
            stop_reason = 'manual-stop'
            break

    with _lock:
        _state.running     = False
        _state.stopped_at  = datetime.now()
        _state.stop_reason = stop_reason
        _state.next_sweep_at = None


def start(params: dict[str, Any]) -> tuple[bool, str]:
    """Spawn the loop thread. Returns (ok, message).

    Idempotent: if a loop is already running, returns (False, "already running").
    """
    global _thread
    with _lock:
        if _state.running:
            return False, 'A take-trades loop is already running.'

        # Fresh state for this run.
        _state.running      = True
        _state.params       = dict(params)
        _state.started_at   = datetime.now()
        _state.stopped_at   = None
        _state.last_sweep_at = None
        _state.next_sweep_at = None
        _state.iterations   = 0
        _state.total_spent  = 0.0
        _state.stop_reason  = None
        _state.last_error   = None
        _state.history.clear()
        _stop_event.clear()

    _thread = threading.Thread(target=_run_loop, args=(params,),
                               name='tt-take-trades-loop', daemon=True)
    _thread.start()
    return True, 'Loop started.'


def stop() -> tuple[bool, str]:
    """Signal the loop to exit. Returns (ok, message)."""
    with _lock:
        if not _state.running:
            return False, 'No loop is running.'
    _stop_event.set()
    return True, 'Stop requested — loop will exit after the current sweep.'
