"""Researcher tables. Created by `init_schema()`, which `job_queue.init_schema()` calls
on tt-api and tt-worker startup, so no migration is needed for these.

    research_control  one row per market: enabled/paused + parameter overrides (JSON)
    research_runs     one row per research run: candidates, leads, collector errors
    research_events   the event catalog: every tournament a collector or the operator
                      added, with its rounds, players and the operator's policy
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..db import get_conn, is_postgres
from ..job_queue import _loads, _norm_ts, iso, utcnow
from .config import ResearchParams

RUN_STATUSES = ('running', 'leads', 'no_leads', 'failed')
POLICIES = ('auto', 'ignore', 'nudge_only', 'cut', 'override')


def _ddl() -> list[str]:
    pk, ts, js = (
        ('BIGSERIAL PRIMARY KEY', 'TIMESTAMPTZ', 'JSONB') if is_postgres()
        else ('INTEGER PRIMARY KEY AUTOINCREMENT', 'TEXT', 'TEXT')
    )
    statuses = ', '.join(f"'{s}'" for s in RUN_STATUSES)
    policies = ', '.join(f"'{s}'" for s in POLICIES)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS research_control (
            market      TEXT PRIMARY KEY,
            enabled     INTEGER NOT NULL DEFAULT 1,
            paused      INTEGER NOT NULL DEFAULT 0,
            params      {js},
            updated_at  {ts},
            updated_by  TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS research_runs (
            id               {pk},
            market           TEXT NOT NULL,
            tourn_date       TEXT NOT NULL,
            job_id           BIGINT,
            status           TEXT NOT NULL CHECK (status IN ({statuses})),
            candidates       {js},
            leads            {js},
            stats            {js},
            collector_errors {js},
            error            TEXT,
            started_at       {ts} NOT NULL,
            ended_at         {ts}
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_research_runs_market ON research_runs(market, id)",
        f"""
        CREATE TABLE IF NOT EXISTS research_events (
            id            {pk},
            source        TEXT NOT NULL,
            source_id     TEXT NOT NULL,
            name          TEXT NOT NULL,
            url           TEXT,
            tier          INTEGER,
            time_zone     TEXT,
            fide_tc       TEXT,
            location      TEXT,
            start_date    TEXT,
            end_date      TEXT,
            rounds        {js},
            players       {js},
            players_hint  TEXT,
            policy        TEXT NOT NULL DEFAULT 'auto' CHECK (policy IN ({policies})),
            policy_value  REAL,
            policy_note   TEXT,
            first_seen_at {ts} NOT NULL,
            last_seen_at  {ts} NOT NULL,
            UNIQUE (source, source_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_research_events_dates ON research_events(start_date, end_date)",
    ]


def init_schema() -> None:
    with get_conn() as conn:
        for stmt in _ddl():
            conn.execute(stmt)


# ── Control ──────────────────────────────────────────────────────────────────

def get_control(market: str) -> dict:
    with get_conn() as conn:
        row = conn.execute('SELECT enabled, paused, params, updated_at, updated_by FROM research_control '
                           'WHERE market = ?', (market,)).fetchone()
    if row is None:
        return {'enabled': True, 'paused': False, 'params': {}, 'updated_at': None, 'updated_by': None}
    return {'enabled': bool(row[0]), 'paused': bool(row[1]), 'params': _loads(row[2]) or {},
            'updated_at': _norm_ts(row[3]), 'updated_by': row[4]}


def params_for(market: str) -> ResearchParams:
    return ResearchParams(**get_control(market)['params'])


def set_params(market: str, params: dict, by: str | None = None) -> ResearchParams:
    validated = ResearchParams(**params)
    overrides = json.dumps(validated.model_dump(exclude_defaults=True))
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO research_control (market, params, updated_at, updated_by) VALUES (?, ?, ?, ?) '
            'ON CONFLICT (market) DO UPDATE SET params = excluded.params, '
            'updated_at = excluded.updated_at, updated_by = excluded.updated_by',
            (market, overrides, iso(utcnow()), by),
        )
    return validated


# ── Runs ─────────────────────────────────────────────────────────────────────

_RUN_COLS = ('id', 'market', 'tourn_date', 'job_id', 'status', 'candidates', 'leads', 'stats',
             'collector_errors', 'error', 'started_at', 'ended_at')
_RUN_JSON = ('candidates', 'leads', 'stats', 'collector_errors')


def _run_row(row: Any) -> dict:
    run = dict(zip(_RUN_COLS, row))
    for f in _RUN_JSON:
        run[f] = _loads(run[f])
    run['started_at'], run['ended_at'] = _norm_ts(run['started_at']), _norm_ts(run['ended_at'])
    return run


def start_run(market: str, tourn_date: str, job_id: int | None = None) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO research_runs (market, tourn_date, job_id, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?) RETURNING id",
            (market, tourn_date, job_id, iso(utcnow())),
        ).fetchone()[0]


def finish_run(run_id: int, status: str, *, candidates: list | None = None, leads: list | None = None,
               stats: dict | None = None, collector_errors: dict | None = None,
               error: str | None = None) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f'unknown run status {status!r}')
    with get_conn() as conn:
        conn.execute(
            'UPDATE research_runs SET status = ?, candidates = ?, leads = ?, stats = ?, '
            'collector_errors = ?, error = ?, ended_at = ? WHERE id = ?',
            (status, json.dumps(candidates or []), json.dumps(leads or []), json.dumps(stats or {}),
             json.dumps(collector_errors or {}), error, iso(utcnow()), run_id),
        )


def latest_run(market: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(f'SELECT {", ".join(_RUN_COLS)} FROM research_runs WHERE market = ? '
                           'ORDER BY id DESC LIMIT 1', (market,)).fetchone()
    return _run_row(row) if row else None


def recent_runs(market: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT id, market, tourn_date, job_id, status, NULL, NULL, stats, collector_errors, error, '
            'started_at, ended_at FROM research_runs WHERE market = ? ORDER BY id DESC LIMIT ?',
            (market, limit),
        ).fetchall()
    return [_run_row(r) for r in rows]


# ── Event catalog ────────────────────────────────────────────────────────────

_EVENT_COLS = ('id', 'source', 'source_id', 'name', 'url', 'tier', 'time_zone', 'fide_tc', 'location',
               'start_date', 'end_date', 'rounds', 'players', 'players_hint', 'policy', 'policy_value',
               'policy_note', 'first_seen_at', 'last_seen_at')
_FACT_COLS = ('name', 'url', 'tier', 'time_zone', 'fide_tc', 'location', 'start_date', 'end_date',
              'rounds', 'players', 'players_hint')


def _event_row(row: Any) -> dict:
    ev = dict(zip(_EVENT_COLS, row))
    ev['rounds'] = _loads(ev['rounds']) or []
    ev['players'] = _loads(ev['players']) or []
    ev['first_seen_at'], ev['last_seen_at'] = _norm_ts(ev['first_seen_at']), _norm_ts(ev['last_seen_at'])
    return ev


def upsert_event(event: dict) -> int:
    """Insert or refresh a collected event by (source, source_id). Never touches the policy."""
    now = iso(utcnow())
    values = [event.get(c) for c in _FACT_COLS]
    values[_FACT_COLS.index('rounds')] = json.dumps(event.get('rounds') or [])
    values[_FACT_COLS.index('players')] = json.dumps(event.get('players') or [])
    cols = ('source', 'source_id', *_FACT_COLS, 'first_seen_at', 'last_seen_at')
    updates = ', '.join(f'{c} = excluded.{c}' for c in (*_FACT_COLS, 'last_seen_at'))
    with get_conn() as conn:
        return conn.execute(
            f'INSERT INTO research_events ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))}) '
            f'ON CONFLICT (source, source_id) DO UPDATE SET {updates} RETURNING id',
            (event['source'], str(event['source_id']), *values, now, now),
        ).fetchone()[0]


def add_operator_event(event: dict) -> int:
    return upsert_event({**event, 'source': 'operator', 'source_id': uuid.uuid4().hex})


def delete_operator_event(event_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("DELETE FROM research_events WHERE id = ? AND source = 'operator' RETURNING id",
                           (event_id,)).fetchone()
    return row is not None


def events_overlapping(first_date: str, last_date: str) -> list[dict]:
    """Events whose date range touches [first_date, last_date] (inclusive, YYYY-MM-DD)."""
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT {", ".join(_EVENT_COLS)} FROM research_events '
            'WHERE start_date <= ? AND COALESCE(end_date, start_date) >= ? ORDER BY start_date, name',
            (last_date, first_date),
        ).fetchall()
    return [_event_row(r) for r in rows]


def get_event(event_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(f'SELECT {", ".join(_EVENT_COLS)} FROM research_events WHERE id = ?',
                           (event_id,)).fetchone()
    return _event_row(row) if row else None


def set_policy(event_id: int, policy: str, value: float | None = None, note: str | None = None) -> bool:
    if policy not in POLICIES:
        raise ValueError(f'unknown policy {policy!r}')
    if policy in ('nudge_only', 'override') and value is None:
        raise ValueError(f'policy {policy!r} needs a value')
    if policy == 'override' and not 0 <= value <= 1:
        raise ValueError('an override is a chance of playing, 0 to 1')
    with get_conn() as conn:
        row = conn.execute(
            'UPDATE research_events SET policy = ?, policy_value = ?, policy_note = ? WHERE id = ? RETURNING id',
            (policy, value if policy in ('nudge_only', 'override') else None, note, event_id),
        ).fetchone()
    return row is not None
