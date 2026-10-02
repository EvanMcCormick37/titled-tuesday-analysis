#!/usr/bin/env python3
"""Weekly data pipeline orchestrator — the single scheduled entry point.

Runs the three pre-trade steps as one resumable state machine:

    scrape_tt → update_broadcasts → run_raw → ADJUSTMENTS_PENDING

Steps are strictly ordered. A failure anywhere halts the pipeline, records
`pipeline_runs.state = 'failed'` with a classified `error_kind`, and the
dashboard / Telegram bot can trigger a resume from the failed step.

Each step is independently idempotent (its underlying DB writes use INSERT
OR REPLACE / DELETE+INSERT patterns), so re-running is always safe.

Usage:
    python scripts/weekly_pipeline.py
    python scripts/weekly_pipeline.py --resume update_broadcasts
    python scripts/weekly_pipeline.py --slug-url https://chess.com/tournament/live/titled-tuesday-blitz-...
"""
import argparse
import json
import sys
import time
import traceback
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Windows consoles default to cp1252; force stdout/stderr to UTF-8 so any
# non-ASCII character printed by downstream scraping scripts (player names,
# arrows in logging) doesn't crash the pipeline on an encoding error.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts' / 'scraping'))

import requests  # for RequestException classification

from src.db import get_conn
from src.pipeline import run_raw, next_tourn_date
from update_titled_tuesday import (
    ListingMissingSlugError,
    _slug_from_url,
    discover_slugs,
    known_slugs,
    process as process_slug,
)
from update_broadcasts import run as run_update_broadcasts

# Strict step order. `resume_from` must be one of these (or None).
STEPS = ('scrape_tt', 'update_broadcasts', 'run_raw')


# ── Date helpers ─────────────────────────────────────────────────────────────

def _most_recent_tuesday(d: date | None = None) -> date:
    """Return the most recent Tuesday on or before `d` (default: today)."""
    d = d or date.today()
    # Monday=0 ... Tuesday=1 ... Sunday=6
    delta = (d.weekday() - 1) % 7
    return d - timedelta(days=delta)


def _now_iso() -> str:
    """UTC now, ISO-8601 with ms precision, matching the DB column defaults."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')[:-4] + 'Z'


# ── pipeline_runs / job_runs helpers ─────────────────────────────────────────

def _get_or_create_pipeline_run(conn, tourn_date: str) -> dict:
    """Return the pipeline_runs row for this tourn_date, creating one if absent."""
    row = conn.execute(
        'SELECT id, tourn_date, state, current_step, failed_step, error_kind, error_message, started_at, completed_at '
        '  FROM pipeline_runs WHERE tourn_date = ?',
        (tourn_date,),
    ).fetchone()
    if row is None:
        conn.execute(
            'INSERT INTO pipeline_runs (tourn_date, state) VALUES (?, ?)',
            (tourn_date, 'running'),
        )
        conn.commit()
        return _get_or_create_pipeline_run(conn, tourn_date)
    cols = ('id', 'tourn_date', 'state', 'current_step', 'failed_step',
            'error_kind', 'error_message', 'started_at', 'completed_at')
    return {c: v for c, v in zip(cols, row)}


def _set_pipeline_state(conn, pr_id: int, **fields) -> None:
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    conn.execute(f'UPDATE pipeline_runs SET {cols} WHERE id = ?',
                 (*fields.values(), pr_id))
    conn.commit()


def _start_job(conn, pr_id: int, job_name: str) -> int:
    row = conn.execute(
        'INSERT INTO job_runs (pipeline_run_id, job_name, status) VALUES (?, ?, ?) RETURNING id',
        (pr_id, job_name, 'running'),
    ).fetchone()
    conn.commit()
    return row[0]


def _end_job(
    conn,
    job_id: int,
    *,
    status: str,
    summary: dict | None = None,
    log_tail: str | None = None,
) -> None:
    conn.execute(
        'UPDATE job_runs SET ended_at = ?, status = ?, summary = ?, log_tail = ? WHERE id = ?',
        (_now_iso(), status, json.dumps(summary) if summary is not None else None, log_tail, job_id),
    )
    conn.commit()


# ── Error classification ─────────────────────────────────────────────────────

def _classify_error(step: str, exc: BaseException) -> str:
    if isinstance(exc, ListingMissingSlugError):
        return 'listing_missing_slug'
    if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError, requests.RequestException)):
        return 'http_error'
    if step == 'run_raw':
        return 'computation_error'
    return 'unknown'


# ── Step implementations ─────────────────────────────────────────────────────

def step_scrape_tt(slug_override: str | None = None) -> dict:
    """Scrape new TT standings. Either resolves via listing or uses an explicit URL/slug.

    Raises ListingMissingSlugError if the listing has no new slug yet; the pipeline
    translates that into the specific error_kind the dashboard needs to show the
    URL input form.
    """
    if slug_override:
        slug = _slug_from_url(slug_override)
        print(f'  scrape_tt: slug override → {slug}')
        process_slug(slug)
        return {'mode': 'override', 'slugs': [slug]}

    seen = known_slugs()
    discovered = discover_slugs()
    new = [s for s in discovered if s not in seen]
    if not new:
        raise ListingMissingSlugError(
            'chess.com listing contains no TT slug not already in the DB '
            f'(latest discovered: {discovered[:3]})'
        )
    print(f'  scrape_tt: {len(new)} new tournament(s): {new}')
    for slug in new:
        process_slug(slug)
        time.sleep(2.0)
    return {'mode': 'listing', 'slugs': new}


def step_update_broadcasts() -> dict:
    return run_update_broadcasts(months=2)


def step_run_raw() -> dict:
    """Run the raw (unadjusted) MC simulation for the upcoming TT."""
    target = next_tourn_date()
    raw = run_raw(target, save_to_db=True)
    return {
        'tourn_date_predicted': target,
        'pool_size':            len(raw.players),
        'n_rows':               int(len(raw.results_raw)),
    }


_STEP_FUNCS = {
    'scrape_tt':         step_scrape_tt,
    'update_broadcasts': step_update_broadcasts,
    'run_raw':           step_run_raw,
}


# ── Driver ───────────────────────────────────────────────────────────────────

def _steps_to_run(resume_from: str | None, state: str, failed_step: str | None,
                  current_step: str | None) -> list[str]:
    """Figure out which steps to run based on the existing row + resume kwarg."""
    if resume_from is not None:
        if resume_from not in STEPS:
            raise ValueError(f'Unknown resume_from step: {resume_from!r}. Valid: {STEPS}')
        start = STEPS.index(resume_from)
    elif state == 'failed':
        if failed_step not in STEPS:
            raise RuntimeError(f'pipeline_runs.state=failed but failed_step={failed_step!r} invalid')
        start = STEPS.index(failed_step)
    elif state == 'running' and current_step in STEPS:
        start = STEPS.index(current_step)
    else:
        start = 0
    return list(STEPS[start:])


def run_pipeline(resume_from: str | None = None, slug_override: str | None = None) -> dict:
    """Run or resume the Tuesday 15:00 pipeline. Returns the final pipeline_runs row."""
    tourn_date = _most_recent_tuesday().isoformat()
    print(f'[weekly_pipeline] tourn_date = {tourn_date}')

    conn = get_conn()
    try:
        pr = _get_or_create_pipeline_run(conn, tourn_date)
        pr_id = pr['id']

        # Fast exits
        if pr['state'] == 'completed' and resume_from is None:
            print(f'[weekly_pipeline] already completed at {pr["completed_at"]}; nothing to do.')
            return pr
        if pr['state'] == 'adjustments_pending' and resume_from is None:
            print('[weekly_pipeline] already in adjustments_pending; nothing to do.')
            return pr

        steps = _steps_to_run(resume_from, pr['state'], pr['failed_step'], pr['current_step'])
        print(f'[weekly_pipeline] steps to run: {steps}')

        # Reset failed/error fields and mark running
        _set_pipeline_state(conn, pr_id,
                            state='running',
                            failed_step=None,
                            error_kind=None,
                            error_message=None,
                            completed_at=None)

        for step in steps:
            _set_pipeline_state(conn, pr_id, current_step=step)
            job_id = _start_job(conn, pr_id, step)
            print(f'[weekly_pipeline] -> {step}')
            try:
                if step == 'scrape_tt':
                    summary = step_scrape_tt(slug_override=slug_override)
                else:
                    summary = _STEP_FUNCS[step]()
            except BaseException as exc:  # noqa: BLE001 — we classify + re-raise selectively
                tb = traceback.format_exc()
                _end_job(conn, job_id, status='failed', summary={'error': str(exc)}, log_tail=tb[-16_000:])
                kind = _classify_error(step, exc)
                _set_pipeline_state(
                    conn, pr_id,
                    state='failed',
                    failed_step=step,
                    error_kind=kind,
                    error_message=f'{type(exc).__name__}: {exc}',
                )
                print(f'[weekly_pipeline] FAIL {step} ({kind}): {exc}')
                return _get_or_create_pipeline_run(conn, tourn_date)
            _end_job(conn, job_id, status='success', summary=summary)
            print(f'[weekly_pipeline] OK {step}: {summary}')

        _set_pipeline_state(conn, pr_id,
                            state='adjustments_pending',
                            current_step=None,
                            completed_at=None)
        print(f'[weekly_pipeline] OK pipeline done -- state = adjustments_pending')
        return _get_or_create_pipeline_run(conn, tourn_date)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--resume', choices=STEPS,
                    help='Resume from this step instead of starting fresh.')
    ap.add_argument('--slug-url',
                    help='Direct chess.com tournament URL (or bare slug) to feed scrape_tt. '
                         'Use this when the listing hasn\'t surfaced the TT yet.')
    args = ap.parse_args()
    final = run_pipeline(resume_from=args.resume, slug_override=args.slug_url)
    print(json.dumps(final, indent=2, default=str))


if __name__ == '__main__':
    main()
