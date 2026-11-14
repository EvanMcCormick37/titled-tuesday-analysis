"""Runs queued trade jobs. `scripts/trade_worker.py` is the service entry point."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import job_queue as q
from .strategies import MARKETS, Market, Strategy, get_strategy
from .trade_plan import Emit, ExecutionResult, Plan, execute_plan

log = logging.getLogger('trade_worker')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_CONSECUTIVE_ERRORS = 3
KILL_LANE_IDLE_TIMEOUT_S = 30.0
OPS_LOG_TAIL_BYTES = 16_000
OPS_STREAM_EVERY_S = 5.0     # how often new subprocess output is copied to the job log
OPS_STREAM_MAX_LINES = 300   # per job; the full tail is still saved when it ends
OPS_LINE_MAX_CHARS = 300
_EPSILON = 0.005  # floating-point tolerance on dollar comparisons


def _default_client() -> Any:
    from kalshi_core import KalshiClient
    return KalshiClient.from_env()


def _make_emit(job: dict) -> Emit:
    job_id, iteration = job['id'], job['iterations']

    def emit(kind: str, message: str, *, level: str = 'info',
             ticker: str | None = None, data: dict | None = None) -> None:
        q.add_event(job_id, kind, message, level=level, ticker=ticker, data=data,
                    iteration=iteration, advance_progress=kind in ('order', 'cancel'))
    return emit


def _outcome_status(result: ExecutionResult) -> str:
    if result.failed == 0:
        return 'succeeded'
    return 'partial' if result.succeeded_any else 'failed'


def _stop(proc: subprocess.Popen) -> None:
    """Terminate a subprocess, killing it if it ignores the request."""
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


class _OutputStream:
    """Copies new complete lines of a running subprocess's output to the job log."""

    def __init__(self, reader: Any, emit: Emit):
        self._reader = reader
        self._emit = emit
        self._partial = b''
        self._sent = 0

    def flush(self) -> None:
        if self._sent >= OPS_STREAM_MAX_LINES:
            return
        data = self._partial + self._reader.read()
        *lines, self._partial = data.split(b'\n')
        for raw in lines:
            # Keep only the last frame of a progress bar redrawn with \r.
            line = raw.rstrip(b'\r').decode('utf-8', errors='replace').rsplit('\r', 1)[-1].strip()
            if not line:
                continue
            self._safe_emit(line[:OPS_LINE_MAX_CHARS])
            self._sent += 1
            if self._sent >= OPS_STREAM_MAX_LINES:
                self._safe_emit('Output continues; the full tail is saved when the job ends.')
                return

    def _safe_emit(self, message: str) -> None:
        # Losing a progress line is fine; crashing the wait loop is not.
        try:
            self._emit('output', message)
        except Exception:  # noqa: BLE001
            log.warning('could not record subprocess output', exc_info=True)


class JobRunner:
    def __init__(
        self,
        client_factory: Callable[[], Any] = _default_client,
        shutting_down: Callable[[], bool] = lambda: False,
        sleep_write: float = 0.10,
        kill_idle_timeout: float = KILL_LANE_IDLE_TIMEOUT_S,
        ops_poll: float = 1.0,
    ):
        self._client_factory = client_factory
        self._shutting_down = shutting_down
        self._sleep_write = sleep_write
        self._kill_idle_timeout = kill_idle_timeout
        self._ops_poll = ops_poll

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self, job: dict) -> None:
        """Run one claimed job to a recorded outcome. Never raises."""
        try:
            if job['strategy'] in q.OPS_STRATEGIES:
                self._run_ops(job)
                return
            market, strategy = get_strategy(job['market'], job['strategy'])
            if job['lane'] == 'priority':
                self._run_kill(job, market, strategy)
            else:
                self._run_normal(job, market, strategy)
        except Exception:  # noqa: BLE001 — every failure must land on the job row
            self._record_crash(job, traceback.format_exc())

    # ── Normal lane ──────────────────────────────────────────────────────────

    def _run_normal(self, job: dict, market: Market, strategy: Strategy) -> None:
        job_id = job['id']
        is_loop = job['loop_interval_min'] is not None
        emit = _make_emit(job)

        if job['not_after'] and q.utcnow() > datetime.fromisoformat(job['not_after']):
            if is_loop and job['iterations'] > 0:
                self._finish_loop(job, 'window-ended')
            else:
                emit('phase', 'Missed its start window; nothing was sent.', level='warning')
                q.finish(job_id, 'expired', error='Missed its start window')
            return

        if strategy.spends and not job['override_stale'] \
                and not market.pipeline_ready(job['tourn_date']):
            reason = ('Predictions are not marked completed for this tournament. '
                      'Approve with the stale-data override to run anyway.')
            emit('phase', reason, level='warning')
            q.hold_for_review(job_id, reason)
            return

        emit('phase', f'Planning {strategy.label.lower()}')
        client = self._client_factory()
        plan = strategy.plan(client, job['tourn_date'], job['params'], emit)
        q.set_plan(job_id, plan.to_dict(), plan.action_count)
        emit('plan', self._plan_line(plan), data={'exposure': plan.exposure,
                                                  'orders': len(plan.orders),
                                                  'cancels': len(plan.cancels)})

        if self._halt_on_cap(job, strategy, plan, is_loop, emit):
            return

        if job['dry_run']:
            emit('phase', 'Dry run complete; nothing was sent.')
            q.finish(job_id, 'succeeded', summary=self._summary(plan, None),
                     iterations_delta=1)
            return

        result = execute_plan(client, plan, emit, self._should_stop(job_id),
                              sleep_write=self._sleep_write)
        self._conclude(job, plan, result, is_loop, emit)

    @staticmethod
    def _plan_line(plan: Plan) -> str:
        return (f'{len(plan.orders)} order(s), {len(plan.cancels)} cancel(s), '
                f'exposure ${plan.exposure:.2f}')

    def _halt_on_cap(self, job: dict, strategy: Strategy, plan: Plan,
                     is_loop: bool, emit: Emit) -> bool:
        if not strategy.spends or job['dry_run']:
            return False
        remaining = job['max_exposure'] - job['spent']
        if plan.exposure <= remaining + _EPSILON:
            return False
        if is_loop:
            emit('phase', f'Sweep of ${plan.exposure:.2f} exceeds the ${remaining:.2f} left '
                          'in the budget; ending loop.', level='warning')
            self._finish_loop(job, 'budget-exhausted', self._summary(plan, None))
        else:
            reason = (f'Fresh plan costs ${plan.exposure:.2f}, above the ${remaining:.2f} '
                      'you approved. Nothing was sent.')
            emit('phase', reason, level='warning')
            q.hold_for_review(job['id'], reason, self._summary(plan, None))
        return True

    def _conclude(self, job: dict, plan: Plan, result: ExecutionResult,
                  is_loop: bool, emit: Emit) -> None:
        job_id = job['id']
        summary = self._summary(plan, result)
        spent = self._spent(plan, result)

        if result.stopped:
            if self._shutting_down() and not q.is_cancel_requested(job_id):
                q.finish(job_id, 'interrupted', summary=summary, spent_delta=spent,
                         iterations_delta=1,
                         error='Worker restarted during this job. Check Kalshi before re-running.')
            else:
                q.finish(job_id, 'cancelled', summary=summary, spent_delta=spent,
                         iterations_delta=1)
            return

        if not is_loop:
            status = _outcome_status(result)
            emit('phase', f'Finished: {result.placed} sent, {result.cancelled} cancelled, '
                          f'{result.failed} failed.',
                 level='error' if result.failed else 'info')
            q.finish(job_id, status, summary=summary, spent_delta=spent, iterations_delta=1)
            return

        self._reschedule_loop(job, plan, summary, spent, errored=False)

    def _reschedule_loop(self, job: dict, plan: Plan | None, summary: dict | None,
                         spent: float, *, errored: bool) -> None:
        job_id = job['id']
        if q.is_cancel_requested(job_id):
            q.finish(job_id, 'cancelled', summary=summary, spent_delta=spent, iterations_delta=1)
            return

        errors = job['consecutive_errors'] + 1 if errored else 0
        if errors >= MAX_CONSECUTIVE_ERRORS:
            q.finish(job_id, 'failed', summary=summary, spent_delta=spent, iterations_delta=1,
                     error=f'{MAX_CONSECUTIVE_ERRORS} consecutive sweeps failed')
            return

        found_orders = plan is not None and bool(plan.orders)
        wait = (timedelta(minutes=job['loop_interval_min']) if found_orders or errored
                else timedelta(hours=job['loop_fallback_hours']))
        next_run = q.utcnow() + wait

        if job['not_after'] and next_run > datetime.fromisoformat(job['not_after']):
            q.finish(job_id, 'succeeded', summary={**(summary or {}), 'stop_reason': 'window-ended'},
                     spent_delta=spent, iterations_delta=1)
            return
        q.requeue_loop(job_id, run_at=next_run, spent_delta=spent,
                       consecutive_errors=errors, summary=summary)

    def _finish_loop(self, job: dict, stop_reason: str, summary: dict | None = None) -> None:
        q.finish(job['id'], 'succeeded',
                 summary={**(summary or job.get('summary') or {}), 'stop_reason': stop_reason})

    # ── Ops jobs (recompute / pipeline) ──────────────────────────────────────

    def _run_ops(self, job: dict) -> None:
        job_id = job['id']
        op = MARKETS[job['market']].ops[job['strategy']]
        emit = _make_emit(job)
        emit('phase', f'{op.label} started')

        # The child writes to one handle and we read through a second, so reading
        # while it runs never moves the child's write position.
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / 'output.log'
            with open(out_path, 'ab') as child_out, open(out_path, 'rb') as reader:
                proc = subprocess.Popen(
                    op.command(job['tourn_date'], job['params']),
                    cwd=PROJECT_ROOT, stdout=child_out, stderr=subprocess.STDOUT,
                    env={**os.environ, 'PYTHONUNBUFFERED': '1'},
                )
                stream = _OutputStream(reader, emit)
                deadline = time.monotonic() + op.timeout_s
                next_stream = time.monotonic() + OPS_STREAM_EVERY_S
                outcome = None
                while proc.poll() is None:
                    if self._shutting_down():
                        outcome = 'interrupted'
                    elif self._cancel_requested(job_id):
                        outcome = 'cancelled'
                    elif time.monotonic() > deadline:
                        outcome = 'failed'
                    if outcome:
                        _stop(proc)
                        break
                    if time.monotonic() >= next_stream:
                        stream.flush()
                        next_stream = time.monotonic() + OPS_STREAM_EVERY_S
                    time.sleep(self._ops_poll)

                reader.seek(max(0, os.fstat(reader.fileno()).st_size - OPS_LOG_TAIL_BYTES))
                tail = reader.read().decode('utf-8', errors='replace')

        emit('log', 'Process output', data={'tail': tail})
        if outcome == 'failed':
            minutes = op.timeout_s / 60
            q.finish(job_id, 'failed', error=f'Timed out after {minutes:g} min', iterations_delta=1)
        elif outcome:
            q.finish(job_id, outcome, iterations_delta=1)
        elif proc.returncode != 0:
            last = tail.strip().splitlines()[-1] if tail.strip() else 'no output'
            emit('phase', last, level='error')
            q.finish(job_id, 'failed', error=last, iterations_delta=1)
        elif (error := op.verify()) is not None:
            emit('phase', error, level='error')
            q.finish(job_id, 'failed', error=error, iterations_delta=1)
        else:
            emit('phase', f'{op.label} finished')
            q.finish(job_id, 'succeeded', iterations_delta=1)

    # ── Priority lane (kill switch) ──────────────────────────────────────────

    def _run_kill(self, job: dict, market: Market, strategy: Strategy) -> None:
        """Cancel resting orders, wait for any running job to stop, then sweep again.

        The second pass catches orders a running job sent between the first
        pass and noticing the stop flag.
        """
        job_id = job['id']
        emit = _make_emit(job)
        client = self._client_factory()
        had_running = q.has_running_trade_job(market.slug)

        plan = strategy.plan(client, job['tourn_date'], job['params'], emit)
        q.set_plan(job_id, plan.to_dict(), plan.action_count)
        total = execute_plan(client, plan, emit, sleep_write=0)

        if had_running:
            emit('phase', 'Waiting for the running job to stop')
            deadline = time.monotonic() + self._kill_idle_timeout
            while q.has_running_trade_job(market.slug) and time.monotonic() < deadline:
                time.sleep(0.25)
            if q.has_running_trade_job(market.slug):
                emit('phase', 'Running job has not stopped yet; check open orders.', level='error')

            second = strategy.plan(client, job['tourn_date'], job['params'], emit)
            result = execute_plan(client, second, emit, sleep_write=0)
            total.cancelled += result.cancelled
            total.failed += result.failed

        q.finish(job_id, _outcome_status(total), summary=total.to_dict(), iterations_delta=1)

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _cancel_requested(job_id: int) -> bool:
        """Cancel check for the ops wait loop. A failed check counts as no cancel
        rather than ending the job and leaving the subprocess running unwatched."""
        try:
            return q.is_cancel_requested(job_id)
        except Exception:  # noqa: BLE001
            log.warning('cancel check for job %s failed; will retry', job_id, exc_info=True)
            return False

    def _should_stop(self, job_id: int) -> Callable[[], bool]:
        return lambda: self._shutting_down() or q.is_cancel_requested(job_id)

    @staticmethod
    def _spent(plan: Plan, result: ExecutionResult) -> float:
        """Dollars to charge against the cap.

        Takers count what filled. Resting bids count their full cost once
        accepted, since they can fill at any time after this job ends.
        """
        return round(result.filled_exposure + result.resting_exposure, 2)

    @staticmethod
    def _summary(plan: Plan, result: ExecutionResult | None) -> dict:
        return {
            'orders_planned':  len(plan.orders),
            'cancels_planned': len(plan.cancels),
            'exposure':        plan.exposure,
            'skipped':         len(plan.skipped),
            'unmatched':       len(plan.unmatched),
            **plan.stats,
            **(result.to_dict() if result else {}),
        }

    def _record_crash(self, job: dict, tb: str) -> None:
        job_id = job['id']
        log.error('job %s crashed:\n%s', job_id, tb)
        try:
            last_line = tb.strip().splitlines()[-1]
            q.add_event(job_id, 'phase', last_line, level='error', data={'traceback': tb},
                        iteration=job['iterations'])
            if job['loop_interval_min'] is not None:
                self._reschedule_loop(job, None, {'last_error': last_line}, 0.0, errored=True)
            else:
                q.finish(job_id, 'failed', error=last_line, iterations_delta=1)
        except Exception:  # noqa: BLE001
            log.exception('could not record failure of job %s', job_id)


class Worker:
    """Polls the queue and runs each claimed job on its own thread."""

    def __init__(
        self,
        client_factory: Callable[[], Any] = _default_client,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 10.0,
        stale_after_seconds: float = 60.0,
        max_threads: int = 8,
        **runner_kwargs: Any,
    ):
        self.worker_id = f'worker-{uuid.uuid4().hex[:8]}'
        self._stop = threading.Event()
        self._poll = poll_seconds
        self._heartbeat_every = heartbeat_seconds
        self._stale_after = stale_after_seconds
        self._runner = JobRunner(client_factory, self._stop.is_set, **runner_kwargs)
        self._pool = concurrent.futures.ThreadPoolExecutor(max_threads, thread_name_prefix='job')
        self._active: dict[int, concurrent.futures.Future] = {}
        self._refreshing: dict[tuple[str, str], concurrent.futures.Future] = {}
        self._last_heartbeat = 0.0
        self._client_factory = client_factory

    def stop(self) -> None:
        self._stop.set()

    def _refresh_snapshot(self, market: Market, kind: str) -> None:
        try:
            payload = market.snapshots[kind].build(self._client_factory(), market.next_event_date())
            q.save_snapshot(market.slug, kind, payload)
        except Exception as e:  # noqa: BLE001 — surfaced to the viewer via the snapshot row
            log.warning('snapshot %s/%s failed: %s', market.slug, kind, e)
            q.save_snapshot(market.slug, kind, None, error=str(e)[:500])

    def _refresh_due_snapshots(self) -> None:
        self._refreshing = {k: f for k, f in self._refreshing.items() if not f.done()}
        for market in MARKETS.values():
            for kind, snap in market.snapshots.items():
                key = (market.slug, kind)
                if key not in self._refreshing and q.snapshot_is_due(market.slug, kind, snap.ttl_seconds):
                    self._refreshing[key] = self._pool.submit(self._refresh_snapshot, market, kind)

    def tick(self) -> int:
        """One poll cycle. Returns how many jobs were started."""
        self._active = {i: f for i, f in self._active.items() if not f.done()}
        now = time.monotonic()
        if now - self._last_heartbeat >= self._heartbeat_every:
            q.heartbeat(list(self._active))
            q.touch_worker(self.worker_id)
            for job_id in q.reap_stale(self._stale_after):
                log.warning('job %s marked interrupted (no heartbeat)', job_id)
            self._last_heartbeat = now

        started = 0
        for lane in ('priority', 'normal'):
            while (job := q.claim_next(self.worker_id, lane)) is not None:
                log.info('starting job %s (%s)', job['id'], job['strategy'])
                self._active[job['id']] = self._pool.submit(self._runner.run, job)
                started += 1
        self._refresh_due_snapshots()
        return started

    def drain(self, timeout: float = 25.0) -> None:
        """Wait for running jobs after stop(), keeping them heartbeating."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [i for i, f in self._active.items() if not f.done()]
            if not pending:
                break
            q.heartbeat(pending)
            time.sleep(0.25)
        self._pool.shutdown(wait=False)

    def run_forever(self) -> None:
        q.init_schema()
        log.info('%s started', self.worker_id)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a DB blip must not kill the service
                log.exception('poll cycle failed')
            self._stop.wait(self._poll)
        log.info('%s draining', self.worker_id)
        self.drain()
