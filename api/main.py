"""tt-api service: JSON API for the dashboard, and host for the built frontend.

Handlers only read the database and add jobs to the queue. Anything slow or
order-sending happens in tt-worker. The one exception is trade preview, which
calls Kalshi read-only so the plan reflects live prices.

Run locally:
    python -m uvicorn api.main:app --reload
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from api import auth
from src import job_queue as q
from src import strategies as strat
from src.db import get_conn, get_engine
from src.pipeline import load_adjustments

WEB_DIST = Path(__file__).resolve().parent.parent / 'web' / 'dist'
WORKER_OFFLINE_AFTER_SECONDS = 45
N_VALUES = (1, 3, 5, 8, 10)
PREDICTIONS_CACHE_SECONDS = 15



@asynccontextmanager
async def _lifespan(_app: FastAPI):
    q.init_schema()
    yield


app = FastAPI(title='Prediction market trading', lifespan=_lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(strat.SubmissionError)
def _submission_error(_request: Request, exc: strat.SubmissionError) -> JSONResponse:
    return JSONResponse(status_code=422, content={'detail': str(exc)})


@app.exception_handler(ValidationError)
def _validation_error(_request: Request, exc: ValidationError) -> JSONResponse:
    message = '; '.join(f'{".".join(map(str, e["loc"]))}: {e["msg"]}' for e in exc.errors())
    return JSONResponse(status_code=422, content={'detail': message})


# ── Auth ─────────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    password: str


@app.post('/api/login')
def login(body: LoginBody, response: Response) -> dict:
    auth.login(body.password, response)
    return {'ok': True}


@app.post('/api/logout')
def logout(response: Response) -> dict:
    auth.logout(response)
    return {'ok': True}


@app.get('/api/me')
def me(request: Request) -> dict:
    return {'authenticated': auth.is_authenticated(request)}


api = APIRouter(prefix='/api', dependencies=[Depends(auth.require_auth)])


def _market(slug: str) -> strat.Market:
    market = strat.MARKETS.get(slug)
    if market is None:
        raise HTTPException(status_code=404, detail='Unknown market')
    return market


def _kalshi() -> Any:
    from kalshi_core import KalshiClient
    return KalshiClient.from_env()


def _ts(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


# ── Markets and status ───────────────────────────────────────────────────────

@api.get('/markets')
def markets() -> list[dict]:
    return [{
        'slug': m.slug,
        'label': m.label,
        'strategies': [{
            'name': s.name, 'label': s.label, 'description': s.description,
            'spends': s.spends, 'can_loop': s.can_loop,
            'schema': s.params_model.model_json_schema(),
        } for s in m.strategies.values()],
    } for m in strat.MARKETS.values()]


@api.get('/markets/{slug}/status')
def status(slug: str) -> dict:
    market = _market(slug)
    event_date = market.next_event_date()
    with get_conn() as conn:
        row = conn.execute(
            'SELECT id, tourn_date, state, current_step, failed_step, error_kind, error_message, '
            'started_at, completed_at FROM pipeline_runs ORDER BY tourn_date DESC LIMIT 1'
        ).fetchone()
    pipeline = None
    if row:
        pipeline = dict(zip(('id', 'tourn_date', 'state', 'current_step', 'failed_step', 'error_kind',
                             'error_message'), row[:7]))
        pipeline['started_at'], pipeline['completed_at'] = _ts(row[7]), _ts(row[8])

    seen = q.worker_last_seen()
    online = bool(seen) and (q.utcnow() - datetime.fromisoformat(seen)).total_seconds() \
        < WORKER_OFFLINE_AFTER_SECONDS
    return {
        'event_date': event_date,
        'event_start': q.iso(market.event_start(event_date)),
        'trade_ready': market.pipeline_ready(event_date),
        'pipeline': pipeline,
        'worker_online': online,
    }


# ── Trading ──────────────────────────────────────────────────────────────────

class PreviewBody(BaseModel):
    strategy: str
    params: dict = Field(default_factory=dict)


class SubmitBody(PreviewBody):
    max_exposure: float | None = None
    run_at: datetime | None = None
    not_after: datetime | None = None
    override_stale: bool = False
    dry_run: bool = False
    loop_interval_min: float | None = None
    loop_fallback_hours: float | None = None


@api.post('/markets/{slug}/preview')
def preview(slug: str, body: PreviewBody) -> dict:
    _market(slug)
    try:
        return strat.preview(_kalshi(), slug, body.strategy, body.params).to_dict()
    except (strat.SubmissionError, ValidationError):
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'Could not build preview: {e}') from e


@api.get('/markets/{slug}/jobs')
def jobs(slug: str, limit: int = 100) -> list[dict]:
    _market(slug)
    return [_job_row(j) for j in q.list_jobs(slug, min(limit, 500))]


def _job_row(job: dict) -> dict:
    """List view: the full plan is left out to keep polling light."""
    plan = job.pop('plan', None) or {}
    job['plan_exposure'] = plan.get('exposure')
    return job


@api.post('/markets/{slug}/jobs')
def submit_job(slug: str, body: SubmitBody) -> dict:
    _market(slug)
    job_id = strat.submit(
        market=slug, strategy=body.strategy, params=body.params, max_exposure=body.max_exposure,
        run_at=body.run_at, not_after=body.not_after, override_stale=body.override_stale,
        dry_run=body.dry_run, loop_interval_min=body.loop_interval_min,
        loop_fallback_hours=body.loop_fallback_hours, created_by='dashboard',
    )
    return {'id': job_id}


@api.post('/markets/{slug}/kill')
def kill(slug: str) -> dict:
    market = _market(slug)
    return {'id': q.enqueue_kill(slug, market.next_event_date(), created_by='dashboard')}


@api.get('/jobs/{job_id}')
def job_detail(job_id: int, after: int = 0) -> dict:
    job = q.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found')
    return {'job': job, 'events': q.list_events(job_id, after)}


@api.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: int) -> dict:
    result = q.request_cancel(job_id)
    if result is None:
        raise HTTPException(status_code=409, detail='Job has already finished')
    return {'status': result}


class ApproveBody(BaseModel):
    max_exposure: float | None = Field(None, gt=0)
    override_stale: bool = False


@api.post('/jobs/{job_id}/approve')
def approve_job(job_id: int, body: ApproveBody) -> dict:
    if not q.approve_review(job_id, max_exposure=body.max_exposure, override_stale=body.override_stale):
        raise HTTPException(status_code=409, detail='Job is not waiting for review')
    return {'ok': True}


# ── Live views ───────────────────────────────────────────────────────────────

@api.get('/markets/{slug}/snapshots/{kind}')
def snapshot(slug: str, kind: str, force: bool = False) -> dict:
    if kind not in _market(slug).snapshots:
        raise HTTPException(status_code=404, detail='Unknown snapshot')
    snap = q.request_snapshot(slug, kind, force)
    return {'payload': snap['payload'], 'error': snap['error'], 'fetched_at': snap['fetched_at']}


# ── Predictions ──────────────────────────────────────────────────────────────

_predictions_cache: dict[bool, tuple[float, pd.DataFrame]] = {}


def _load_predictions(adjusted: bool) -> pd.DataFrame:
    cached = _predictions_cache.get(adjusted)
    if cached and time.monotonic() - cached[0] < PREDICTIONS_CACHE_SECONDS:
        return cached[1]
    table = 'latest_model_predictions' if adjusted else 'latest_model_predictions_raw'
    df = pd.read_sql_query(f'SELECT * FROM {table}', get_engine())
    with get_conn() as conn:
        names = dict(conn.execute('SELECT username, player_name FROM player_information '
                                  'WHERE player_name IS NOT NULL').fetchall())
    if not df.empty:
        df['player'] = df['username'].map(names).fillna(df['username'])
    _predictions_cache[adjusted] = (time.monotonic(), df)
    return df


@api.get('/markets/{slug}/predictions')
def predictions(slug: str, n: int = 1, adjusted: bool = True, limit: int = 30) -> dict:
    _market(slug)
    if n not in N_VALUES:
        raise HTTPException(status_code=422, detail=f'n must be one of {N_VALUES}')
    df = _load_predictions(adjusted)
    given = f'P_top{n}_given_play'
    if df.empty or given not in df.columns:
        return {'rows': [], 'pool_size': 0, 'tourn_date': None}
    view = df.assign(p_top=df['p_participate'] * df[given]).nlargest(min(limit, 200), 'p_top')
    rows = [{
        'player': r.player, 'p_participate': r.p_participate, 'p_given_play': getattr(r, 'p_given'),
        'p_top': r.p_top, 'yes_price': round(r.p_top * (2 / 3) * 100, 1),
        'no_price': round(max((1 - r.p_top * (3 / 2)) * 100, 0), 1),
    } for r in view.rename(columns={given: 'p_given'}).itertuples()]
    date = df['tourn_date'].iloc[0] if 'tourn_date' in df.columns else None
    return {'rows': rows, 'pool_size': len(df), 'tourn_date': _ts(date)}


# ── Attendance adjustments ───────────────────────────────────────────────────

class AdjustmentsBody(BaseModel):
    cut_players: list[str] = Field(default_factory=list)
    keep_players: list[str] = Field(default_factory=list)
    p_participate_overrides: dict[str, float] = Field(default_factory=dict)
    p_nudges: dict[str, float] = Field(default_factory=dict)
    global_nudge: float = 0.0
    recompute: bool = False


@api.get('/markets/{slug}/adjustments')
def get_adjustments(slug: str) -> dict:
    market = _market(slug)
    date = market.next_event_date()
    with get_conn() as conn:
        names = [r[0] for r in conn.execute(
            'SELECT DISTINCT player_name FROM player_information WHERE player_name IS NOT NULL '
            'ORDER BY player_name').fetchall()]
    return {'event_date': date, 'adjustments': load_adjustments(date), 'players': names}


@api.put('/markets/{slug}/adjustments')
def save_adjustments(slug: str, body: AdjustmentsBody) -> dict:
    market = _market(slug)
    bad = [k for k, v in body.p_participate_overrides.items() if not 0 <= v <= 1]
    if bad:
        raise HTTPException(status_code=422, detail=f'Overrides must be 0 to 1: {", ".join(bad)}')
    date = market.next_event_date()

    rows: list[tuple[str, str, float | None]] = [
        *((name, 'cut', None) for name in body.cut_players),
        *((name, 'keep', None) for name in body.keep_players),
        *((name, 'override', v) for name, v in body.p_participate_overrides.items()),
        *((name, 'nudge', v) for name, v in body.p_nudges.items()),
    ]
    if body.global_nudge:
        rows.append(('', 'global_nudge', body.global_nudge))

    with get_conn() as conn:
        conn.execute('DELETE FROM manual_adjustments WHERE tourn_date = ?', (date,))
        conn.executemany(
            'INSERT INTO manual_adjustments (tourn_date, player_name, adjustment_type, value, created_by) '
            'VALUES (?, ?, ?, ?, ?)',
            [(date, name, kind, value, 'dashboard') for name, kind, value in rows],
        )
    job_id = strat.submit_ops(slug, 'recompute', created_by='dashboard') if body.recompute else None
    return {'saved': len(rows), 'job_id': job_id}


# ── Pipeline and logs ────────────────────────────────────────────────────────

class OpsBody(BaseModel):
    slug_url: str | None = None


@api.post('/markets/{slug}/recompute')
def recompute(slug: str) -> dict:
    _market(slug)
    return {'id': strat.submit_ops(slug, 'recompute', created_by='dashboard')}


@api.post('/markets/{slug}/pipeline/run')
def run_pipeline(slug: str, body: OpsBody) -> dict:
    _market(slug)
    params = {'slug_url': body.slug_url.strip()} if body.slug_url and body.slug_url.strip() else {}
    return {'id': strat.submit_ops(slug, 'run_pipeline', params, created_by='dashboard')}


_JOB_RUN_COLS = ('id', 'pipeline_run_id', 'job_name', 'status', 'started_at', 'ended_at', 'summary', 'log_tail')


def _job_run(row: Any) -> dict:
    item = dict(zip(_JOB_RUN_COLS, row))
    item['started_at'], item['ended_at'] = _ts(item['started_at']), _ts(item['ended_at'])
    if isinstance(item['summary'], str):
        try:
            item['summary'] = json.loads(item['summary'])
        except ValueError:
            pass
    return item


@api.get('/markets/{slug}/pipeline')
def pipeline_history(slug: str) -> dict:
    _market(slug)
    cols = ('id', 'tourn_date', 'state', 'failed_step', 'error_kind', 'started_at', 'completed_at')
    with get_conn() as conn:
        rows = conn.execute(f'SELECT {", ".join(cols)} FROM pipeline_runs '
                            'ORDER BY tourn_date DESC LIMIT 10').fetchall()
        runs = [dict(zip(cols, r)) for r in rows]
        for run in runs:
            run['started_at'], run['completed_at'] = _ts(run['started_at']), _ts(run['completed_at'])
        steps = []
        if runs:
            steps = [_job_run(r) for r in conn.execute(
                f'SELECT {", ".join(_JOB_RUN_COLS)} FROM job_runs WHERE pipeline_run_id = ? ORDER BY id',
                (runs[0]['id'],)).fetchall()]
    return {'runs': runs, 'steps': steps}


@api.get('/logs')
def logs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(f'SELECT {", ".join(_JOB_RUN_COLS)} FROM job_runs ORDER BY id DESC LIMIT ?',
                            (min(limit, 500),)).fetchall()
    return [_job_run(r) for r in rows]


app.include_router(api)


# ── Frontend ─────────────────────────────────────────────────────────────────

if (WEB_DIST / 'assets').exists():
    app.mount('/assets', StaticFiles(directory=WEB_DIST / 'assets'), name='assets')


@app.get('/{path:path}', include_in_schema=False)
def spa(path: str) -> FileResponse:
    if path.startswith('api/') or not (WEB_DIST / 'index.html').exists():
        raise HTTPException(status_code=404, detail='Not found')
    return FileResponse(WEB_DIST / 'index.html')
