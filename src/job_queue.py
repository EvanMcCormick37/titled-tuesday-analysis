"""Postgres-backed trade job queue (SQLite for local development).

Rules enforced here rather than in callers:
  - At most one `normal`-lane job runs per market. A partial UNIQUE index makes
    this a database guarantee, not just a worker convention.
  - `priority`-lane jobs (the kill switch) ignore that limit.
  - A live job is never retried automatically. If a worker dies mid-job the job
    becomes `interrupted` and waits for a human, to avoid double orders.

A job waiting for a future `run_at` is simply `queued`; the UI shows that as
"scheduled". Loops use the same mechanism, re-queueing themselves between
sweeps so they never hold the market's lane while idle.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import get_conn, is_postgres

ACTIVE_STATUSES = ('queued', 'running', 'needs_review')
TERMINAL_STATUSES = ('succeeded', 'partial', 'failed', 'cancelled', 'interrupted', 'expired')

# Non-trading jobs. They share the market's lane on purpose: a recompute briefly
# empties the predictions table, so a trade must never plan during one. The kill
# switch leaves them alone because they can't place orders.
OPS_STRATEGIES = ('recompute', 'run_pipeline')
_OPS_SQL = ', '.join(f"'{s}'" for s in OPS_STRATEGIES)

_JOB_COLUMNS = (
    'id', 'market', 'strategy', 'tourn_date', 'params', 'dry_run', 'lane', 'status',
    'run_at', 'not_after', 'max_exposure', 'override_stale', 'loop_interval_min',
    'loop_fallback_hours', 'iterations', 'spent', 'consecutive_errors', 'plan', 'summary',
    'progress_done', 'progress_total', 'review_reason', 'error', 'cancel_requested',
    'created_by', 'created_at', 'claimed_by', 'started_at', 'heartbeat_at', 'ended_at',
)
_COLS = ', '.join(_JOB_COLUMNS)
_JSON_FIELDS = ('params', 'plan', 'summary')
_BOOL_FIELDS = ('dry_run', 'override_stale', 'cancel_requested')
_TS_FIELDS = ('run_at', 'not_after', 'created_at', 'started_at', 'heartbeat_at', 'ended_at')


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Fixed-width UTC ISO string, so text comparison on SQLite is chronological."""
    if dt.tzinfo is None:
        raise ValueError('naive datetime')
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def _norm_ts(value: Any) -> str | None:
    if value is None:
        return None
    return iso(value) if isinstance(value, datetime) else str(value)


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _row_to_job(row: Any) -> dict:
    job = dict(zip(_JOB_COLUMNS, row))
    for f in _JSON_FIELDS:
        job[f] = _loads(job[f])
    for f in _BOOL_FIELDS:
        job[f] = bool(job[f])
    for f in _TS_FIELDS:
        job[f] = _norm_ts(job[f])
    return job


def _is_unique_violation(exc: Exception) -> bool:
    name = type(exc).__name__
    return name == 'UniqueViolation' or (name == 'IntegrityError' and 'UNIQUE' in str(exc))


# ── Schema ───────────────────────────────────────────────────────────────────

def _ddl() -> list[str]:
    pk, ts, js = (
        ('BIGSERIAL PRIMARY KEY', 'TIMESTAMPTZ', 'JSONB') if is_postgres()
        else ('INTEGER PRIMARY KEY AUTOINCREMENT', 'TEXT', 'TEXT')
    )
    statuses = ', '.join(f"'{s}'" for s in ACTIVE_STATUSES + TERMINAL_STATUSES)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS trade_jobs (
            id                  {pk},
            market              TEXT NOT NULL,
            strategy            TEXT NOT NULL,
            tourn_date          TEXT NOT NULL,
            params              {js} NOT NULL,
            dry_run             INTEGER NOT NULL DEFAULT 0,
            lane                TEXT NOT NULL DEFAULT 'normal'
                                CHECK (lane IN ('normal','priority')),
            status              TEXT NOT NULL DEFAULT 'queued'
                                CHECK (status IN ({statuses})),
            run_at              {ts} NOT NULL,
            not_after           {ts},
            max_exposure        REAL,
            override_stale      INTEGER NOT NULL DEFAULT 0,
            loop_interval_min   REAL,
            loop_fallback_hours REAL,
            iterations          INTEGER NOT NULL DEFAULT 0,
            spent               REAL NOT NULL DEFAULT 0,
            consecutive_errors  INTEGER NOT NULL DEFAULT 0,
            plan                {js},
            summary             {js},
            progress_done       INTEGER NOT NULL DEFAULT 0,
            progress_total      INTEGER NOT NULL DEFAULT 0,
            review_reason       TEXT,
            error               TEXT,
            cancel_requested    INTEGER NOT NULL DEFAULT 0,
            created_by          TEXT,
            created_at          {ts} NOT NULL,
            claimed_by          TEXT,
            started_at          {ts},
            heartbeat_at        {ts},
            ended_at            {ts}
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trade_jobs_status ON trade_jobs(status, run_at)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_jobs_one_running
        ON trade_jobs(market) WHERE status = 'running' AND lane = 'normal'
        """,
        f"""
        CREATE TABLE IF NOT EXISTS trade_job_events (
            id          {pk},
            job_id      BIGINT NOT NULL REFERENCES trade_jobs(id),
            ts          {ts} NOT NULL,
            iteration   INTEGER NOT NULL DEFAULT 0,
            kind        TEXT NOT NULL,
            level       TEXT NOT NULL DEFAULT 'info',
            ticker      TEXT,
            message     TEXT NOT NULL,
            data        {js}
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trade_job_events_job ON trade_job_events(job_id, id)",
        f"""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            market       TEXT NOT NULL,
            kind         TEXT NOT NULL,
            payload      {js},
            error        TEXT,
            fetched_at   {ts},
            requested_at {ts},
            force        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (market, kind)
        )
        """,
    ]


def init_schema() -> None:
    with get_conn() as conn:
        for stmt in _ddl():
            conn.execute(stmt)


# ── Writes: submission ───────────────────────────────────────────────────────

def enqueue(
    *,
    market: str,
    strategy: str,
    tourn_date: str,
    params: dict,
    dry_run: bool = False,
    lane: str = 'normal',
    run_at: datetime | None = None,
    not_after: datetime | None = None,
    max_exposure: float | None = None,
    override_stale: bool = False,
    loop_interval_min: float | None = None,
    loop_fallback_hours: float | None = None,
    created_by: str | None = None,
    conn: Any = None,
) -> int:
    now = utcnow()
    sql = (
        'INSERT INTO trade_jobs (market, strategy, tourn_date, params, dry_run, lane, status, '
        'run_at, not_after, max_exposure, override_stale, loop_interval_min, '
        'loop_fallback_hours, created_by, created_at) '
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"
    )
    args = (
        market, strategy, tourn_date, json.dumps(params), int(dry_run), lane,
        iso(run_at or now), iso(not_after) if not_after else None, max_exposure,
        int(override_stale), loop_interval_min, loop_fallback_hours, created_by, iso(now),
    )
    if conn is not None:
        return conn.execute(sql, args).fetchone()[0]
    with get_conn() as c:
        return c.execute(sql, args).fetchone()[0]


def enqueue_kill(market: str, tourn_date: str, created_by: str | None = None) -> int:
    """Kill switch: drop queued work, stop running work, then cancel resting orders.

    Atomic, so no queued job can start between clearing the queue and inserting
    the priority job.
    """
    now = iso(utcnow())
    with get_conn() as conn:
        conn.execute(
            "UPDATE trade_jobs SET status='cancelled', ended_at=?, error='Cancelled by kill switch' "
            f"WHERE market=? AND lane='normal' AND strategy NOT IN ({_OPS_SQL}) "
            "AND status IN ('queued','needs_review')",
            (now, market),
        )
        conn.execute(
            "UPDATE trade_jobs SET cancel_requested=1 "
            f"WHERE market=? AND lane='normal' AND strategy NOT IN ({_OPS_SQL}) "
            "AND status='running'",
            (market,),
        )
        return enqueue(
            market=market, strategy='cancel_bids', tourn_date=tourn_date, params={},
            lane='priority', created_by=created_by, conn=conn,
        )


def request_cancel(job_id: int) -> str | None:
    """Cancel a waiting job immediately or flag a running one to stop.

    Returns the resulting status, or None if the job is already finished.
    """
    now = iso(utcnow())
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE trade_jobs SET status='cancelled', ended_at=? "
            "WHERE id=? AND status IN ('queued','needs_review') RETURNING status",
            (now, job_id),
        ).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "UPDATE trade_jobs SET cancel_requested=1 "
            "WHERE id=? AND status='running' RETURNING status",
            (job_id,),
        ).fetchone()
        return row[0] if row else None


def approve_review(
    job_id: int,
    *,
    max_exposure: float | None = None,
    override_stale: bool = False,
    window_minutes: float = 15,
) -> bool:
    """Send a `needs_review` job back to the queue, optionally raising its cap."""
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE trade_jobs SET status='queued', review_reason=NULL, run_at=?, not_after=?, "
            "max_exposure=COALESCE(?, max_exposure), "
            "override_stale=CASE WHEN ?=1 THEN 1 ELSE override_stale END "
            "WHERE id=? AND status='needs_review' RETURNING id",
            (iso(now), iso(now + timedelta(minutes=window_minutes)), max_exposure,
             int(override_stale), job_id),
        ).fetchone()
    return row is not None


# ── Writes: worker ───────────────────────────────────────────────────────────

def claim_next(worker_id: str, lane: str = 'normal') -> dict | None:
    """Atomically move the next due job to `running`, or return None."""
    now = iso(utcnow())
    sql = f"""
        UPDATE trade_jobs
        SET status='running', claimed_by=?, heartbeat_at=?, started_at=COALESCE(started_at, ?)
        WHERE status='queued' AND id = (
            SELECT j.id FROM trade_jobs j
            WHERE j.status='queued' AND j.lane=? AND j.run_at <= ?
              AND (j.lane='priority' OR NOT EXISTS (
                    SELECT 1 FROM trade_jobs r
                    WHERE r.market=j.market AND r.lane='normal' AND r.status='running'))
            ORDER BY j.run_at, j.id
            LIMIT 1
        )
        RETURNING {_COLS}
    """
    try:
        with get_conn() as conn:
            row = conn.execute(sql, (worker_id, now, now, lane, now)).fetchone()
    except Exception as e:
        if _is_unique_violation(e):
            return None
        raise
    return _row_to_job(row) if row else None


def heartbeat(job_ids: list[int]) -> None:
    if not job_ids:
        return
    now = iso(utcnow())
    with get_conn() as conn:
        conn.executemany(
            "UPDATE trade_jobs SET heartbeat_at=? WHERE id=? AND status='running'",
            [(now, i) for i in job_ids],
        )


def reap_stale(max_age_seconds: float = 60) -> list[int]:
    """Mark running jobs whose worker stopped heartbeating as `interrupted`."""
    now = utcnow()
    cutoff = iso(now - timedelta(seconds=max_age_seconds))
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE trade_jobs SET status='interrupted', ended_at=?, "
            "error='Worker stopped during this job. Check Kalshi before re-running.' "
            "WHERE status='running' AND heartbeat_at < ? RETURNING id",
            (iso(now), cutoff),
        ).fetchall()
    return [r[0] for r in rows]


def set_plan(job_id: int, plan: dict, progress_total: int) -> None:
    with get_conn() as conn:
        conn.execute(
            'UPDATE trade_jobs SET plan=?, progress_done=0, progress_total=? WHERE id=?',
            (json.dumps(plan), progress_total, job_id),
        )


def finish(
    job_id: int,
    status: str,
    *,
    summary: dict | None = None,
    error: str | None = None,
    spent_delta: float = 0.0,
    iterations_delta: int = 0,
) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f'{status!r} is not a terminal status')
    with get_conn() as conn:
        conn.execute(
            'UPDATE trade_jobs SET status=?, summary=?, error=?, ended_at=?, '
            'spent=spent+?, iterations=iterations+? WHERE id=?',
            (status, json.dumps(summary) if summary is not None else None, error,
             iso(utcnow()), spent_delta, iterations_delta, job_id),
        )


def hold_for_review(job_id: int, reason: str, summary: dict | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE trade_jobs SET status='needs_review', review_reason=?, summary=? WHERE id=?",
            (reason, json.dumps(summary) if summary is not None else None, job_id),
        )


def requeue_loop(
    job_id: int,
    *,
    run_at: datetime,
    spent_delta: float,
    consecutive_errors: int,
    summary: dict | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE trade_jobs SET status='queued', run_at=?, spent=spent+?, iterations=iterations+1, "
            'consecutive_errors=?, summary=? WHERE id=?',
            (iso(run_at), spent_delta, consecutive_errors,
             json.dumps(summary) if summary is not None else None, job_id),
        )


def add_event(
    job_id: int,
    kind: str,
    message: str,
    *,
    level: str = 'info',
    ticker: str | None = None,
    data: dict | None = None,
    iteration: int = 0,
    advance_progress: bool = False,
) -> None:
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO trade_job_events (job_id, ts, iteration, kind, level, ticker, message, data) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (job_id, iso(utcnow()), iteration, kind, level, ticker, message,
             json.dumps(data) if data is not None else None),
        )
        if advance_progress:
            conn.execute('UPDATE trade_jobs SET progress_done=progress_done+1 WHERE id=?', (job_id,))


# ── Reads ────────────────────────────────────────────────────────────────────

def get_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(f'SELECT {_COLS} FROM trade_jobs WHERE id=?', (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def is_cancel_requested(job_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute('SELECT cancel_requested FROM trade_jobs WHERE id=?', (job_id,)).fetchone()
    return bool(row and row[0])


def has_running_trade_job(market: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trade_jobs WHERE market=? AND lane='normal' AND status='running' "
            f"AND strategy NOT IN ({_OPS_SQL}) LIMIT 1",
            (market,),
        ).fetchone()
    return row is not None


def has_active_job(market: str, strategy: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trade_jobs WHERE market=? AND strategy=? "
            "AND status IN ('queued','running') LIMIT 1",
            (market, strategy),
        ).fetchone()
    return row is not None


# ── Snapshots ────────────────────────────────────────────────────────────────
# The worker refreshes live Kalshi views here so page loads never call Kalshi.
# A kind is only refreshed while someone has asked for it recently.

_SNAPSHOT_COLS = ('market', 'kind', 'payload', 'error', 'fetched_at', 'requested_at', 'force')


def _row_to_snapshot(row: Any) -> dict:
    snap = dict(zip(_SNAPSHOT_COLS, row))
    snap['payload'] = _loads(snap['payload'])
    snap['fetched_at'] = _norm_ts(snap['fetched_at'])
    snap['requested_at'] = _norm_ts(snap['requested_at'])
    return snap


def request_snapshot(market: str, kind: str, force: bool = False) -> dict:
    """Record that a viewer wants this snapshot and return the latest copy."""
    now = iso(utcnow())
    with get_conn() as conn:
        row = conn.execute(
            'INSERT INTO market_snapshots (market, kind, requested_at, force) VALUES (?, ?, ?, ?) '
            'ON CONFLICT (market, kind) DO UPDATE SET requested_at = excluded.requested_at, '
            'force = CASE WHEN excluded.force = 1 THEN 1 ELSE market_snapshots.force END '
            f'RETURNING {", ".join(_SNAPSHOT_COLS)}',
            (market, kind, now, int(force)),
        ).fetchone()
    return _row_to_snapshot(row)


def snapshot_is_due(market: str, kind: str, ttl_seconds: float, viewer_window: float = 120) -> bool:
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute(
            f'SELECT {", ".join(_SNAPSHOT_COLS)} FROM market_snapshots WHERE market=? AND kind=?',
            (market, kind),
        ).fetchone()
    if row is None:
        return False
    snap = _row_to_snapshot(row)
    if not snap['requested_at'] or \
            (now - datetime.fromisoformat(snap['requested_at'])).total_seconds() > viewer_window:
        return False
    if snap['force'] or not snap['fetched_at']:
        return True
    return (now - datetime.fromisoformat(snap['fetched_at'])).total_seconds() >= ttl_seconds


def save_snapshot(market: str, kind: str, payload: dict | None, error: str | None = None) -> None:
    """Store a refresh result. On error the last good payload is kept."""
    now = iso(utcnow())
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO market_snapshots (market, kind, payload, error, fetched_at, force) '
            'VALUES (?, ?, ?, ?, ?, 0) '
            'ON CONFLICT (market, kind) DO UPDATE SET '
            'payload = COALESCE(excluded.payload, market_snapshots.payload), '
            'error = excluded.error, fetched_at = excluded.fetched_at, force = 0',
            (market, kind, json.dumps(payload) if payload is not None else None, error, now),
        )


def touch_worker(worker_id: str) -> None:
    save_snapshot('_system', 'worker', {'worker_id': worker_id})


def worker_last_seen() -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM market_snapshots WHERE market='_system' AND kind='worker'"
        ).fetchone()
    return _norm_ts(row[0]) if row else None


def list_jobs(market: str | None = None, limit: int = 100) -> list[dict]:
    """Active jobs first (soonest to run first), then most recently finished."""
    active = ', '.join(f"'{s}'" for s in ACTIVE_STATUSES)
    where = 'WHERE market=?' if market else ''
    args: tuple = (market, limit) if market else (limit,)
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT {_COLS} FROM trade_jobs {where} '
            f'ORDER BY CASE WHEN status IN ({active}) THEN 0 ELSE 1 END, '
            f'CASE WHEN status IN ({active}) THEN run_at END ASC, id DESC LIMIT ?',
            args,
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def list_events(job_id: int, after_id: int = 0, limit: int = 1000) -> list[dict]:
    cols = ('id', 'job_id', 'ts', 'iteration', 'kind', 'level', 'ticker', 'message', 'data')
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT {", ".join(cols)} FROM trade_job_events '
            'WHERE job_id=? AND id>? ORDER BY id LIMIT ?',
            (job_id, after_id, limit),
        ).fetchall()
    events = [dict(zip(cols, r)) for r in rows]
    for e in events:
        e['data'] = _loads(e['data'])
        e['ts'] = _norm_ts(e['ts'])
    return events
