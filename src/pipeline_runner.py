"""Resumable weekly pipeline shared by every market.

Steps are strictly ordered. A failure halts the run, records
`pipeline_runs.state = 'failed'` with a classified `error_kind`, and the dashboard
can resume from the failed step. Each step must be idempotent.
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .db import get_conn
from .markets import MarketSpec

_RUN_COLUMNS = ('id', 'tourn_date', 'state', 'current_step', 'failed_step',
                'error_kind', 'error_message', 'started_at', 'completed_at')


@dataclass(frozen=True)
class PipelineDef:
    spec: MarketSpec
    steps: tuple[str, ...]
    run_step: Callable[[str, str | None], dict]         # (step, slug_override) -> summary
    classify_error: Callable[[str, BaseException], str]


def _now_iso() -> str:
    """UTC now, ISO-8601 with ms precision, matching the DB column defaults."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')[:-4] + 'Z'


def _get_or_create_run(conn, market: str, tourn_date: str) -> dict:
    query = (f'SELECT {", ".join(_RUN_COLUMNS)} FROM pipeline_runs '
             'WHERE market = ? AND tourn_date = ?')
    row = conn.execute(query, (market, tourn_date)).fetchone()
    if row is None:
        conn.execute('INSERT INTO pipeline_runs (market, tourn_date, state) VALUES (?, ?, ?)',
                     (market, tourn_date, 'running'))
        conn.commit()
        row = conn.execute(query, (market, tourn_date)).fetchone()
    return dict(zip(_RUN_COLUMNS, row))


def _set_state(conn, run_id: int, **fields) -> None:
    cols = ', '.join(f'{k} = ?' for k in fields)
    conn.execute(f'UPDATE pipeline_runs SET {cols} WHERE id = ?', (*fields.values(), run_id))
    conn.commit()


def _start_job(conn, run_id: int, job_name: str) -> int:
    row = conn.execute(
        'INSERT INTO job_runs (pipeline_run_id, job_name, status) VALUES (?, ?, ?) RETURNING id',
        (run_id, job_name, 'running'),
    ).fetchone()
    conn.commit()
    return row[0]


def _end_job(conn, job_id: int, *, status: str, summary: dict | None = None,
             log_tail: str | None = None) -> None:
    conn.execute(
        'UPDATE job_runs SET ended_at = ?, status = ?, summary = ?, log_tail = ? WHERE id = ?',
        (_now_iso(), status, json.dumps(summary) if summary is not None else None, log_tail, job_id),
    )
    conn.commit()


def _steps_to_run(steps: tuple[str, ...], resume_from: str | None, state: str,
                  failed_step: str | None, current_step: str | None) -> list[str]:
    if resume_from is not None:
        if resume_from not in steps:
            raise ValueError(f'Unknown resume_from step: {resume_from!r}. Valid: {steps}')
        start = steps.index(resume_from)
    elif state == 'failed':
        if failed_step not in steps:
            raise RuntimeError(f'pipeline_runs.state=failed but failed_step={failed_step!r} invalid')
        start = steps.index(failed_step)
    elif state == 'running' and current_step in steps:
        start = steps.index(current_step)
    else:
        start = 0
    return list(steps[start:])


def run_pipeline(defn: PipelineDef, resume_from: str | None = None,
                 slug_override: str | None = None) -> dict:
    """Run or resume this week's pipeline. Returns the final pipeline_runs row."""
    market = defn.spec.slug
    tourn_date = defn.spec.most_recent_event().isoformat()
    print(f'[pipeline:{market}] tourn_date = {tourn_date}')

    conn = get_conn()
    try:
        run = _get_or_create_run(conn, market, tourn_date)
        run_id = run['id']

        if resume_from is None and run['state'] in ('completed', 'adjustments_pending'):
            print(f'[pipeline:{market}] already {run["state"]}; nothing to do.')
            return run

        steps = _steps_to_run(defn.steps, resume_from, run['state'], run['failed_step'], run['current_step'])
        print(f'[pipeline:{market}] steps to run: {steps}')

        _set_state(conn, run_id, state='running', failed_step=None, error_kind=None,
                   error_message=None, completed_at=None)

        for step in steps:
            _set_state(conn, run_id, current_step=step)
            job_id = _start_job(conn, run_id, step)
            print(f'[pipeline:{market}] -> {step}')
            try:
                summary = defn.run_step(step, slug_override)
            except BaseException as exc:  # noqa: BLE001 — recorded so the UI can offer a retry
                _end_job(conn, job_id, status='failed', summary={'error': str(exc)},
                         log_tail=traceback.format_exc()[-16_000:])
                kind = defn.classify_error(step, exc)
                _set_state(conn, run_id, state='failed', failed_step=step, error_kind=kind,
                           error_message=f'{type(exc).__name__}: {exc}')
                print(f'[pipeline:{market}] FAIL {step} ({kind}): {exc}')
                return _get_or_create_run(conn, market, tourn_date)
            _end_job(conn, job_id, status='success', summary=summary)
            print(f'[pipeline:{market}] OK {step}: {summary}')

        _set_state(conn, run_id, state='adjustments_pending', current_step=None, completed_at=None)
        print(f'[pipeline:{market}] OK pipeline done -- state = adjustments_pending')
        return _get_or_create_run(conn, market, tourn_date)
    finally:
        conn.close()
