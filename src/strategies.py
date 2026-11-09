"""Registry of markets and their strategies.

The API, forms, and worker are all driven by this registry, so adding a market
means registering a `Market` here rather than editing those layers.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import job_queue, market_views, trading
from .trade_plan import Emit, Plan


class _Params(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PlaceBidsParams(_Params):
    count: int = Field(200, ge=1, le=10_000, title='Contracts per bid')
    markup: float = Field(1.5, ge=1.0, le=10.0, title='Markup', description='Bid = fair / markup')
    max_discount: float = Field(15, ge=0, le=50, title='Max discount (¢)',
                                description='Hard cap on cents below fair')
    side_filter: Literal['yes', 'no'] | None = Field(None, title='Side')


class FadeNoBidsParams(_Params):
    discount_cents: int = Field(15, ge=1, le=50, title='Discount (¢)',
                                description='Cents below fair NO')
    count: int = Field(200, ge=1, le=10_000, title='Contracts per bid')
    minutes_after_start: int = Field(10, ge=0, le=180, title='Expiry (min after 11:00 ET)')


class TakeTradesParams(_Params):
    min_roi: float = Field(1.5, ge=1.0, le=5.0, title='Min ROI')
    min_qty: int = Field(25, ge=1, le=1_000, title='Min quantity',
                         description='Thinner levels are skipped as likely decoys')
    max_qty: int = Field(200, ge=1, le=10_000, title='Max contracts per level')
    best_per_event: bool = Field(True, title='Best per event',
                                 description='Only the highest-ROI trade per top-N category')


class CancelBidsParams(_Params):
    pass


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str
    description: str
    params_model: type[_Params]
    plan: Callable[..., Plan]
    spends: bool = True          # False: needs no exposure cap or fresh predictions
    can_loop: bool = False


@dataclass(frozen=True)
class OpsJob:
    """A non-trading job, run as a subprocess so its CPU use can't stall orders."""
    name: str
    label: str
    command: Callable[[str, dict], list[str]]
    verify: Callable[[], str | None] = lambda: None   # returns an error if the run failed


@dataclass(frozen=True)
class Snapshot:
    build: Callable[[Any, str], dict]
    ttl_seconds: float


@dataclass(frozen=True)
class Market:
    slug: str
    label: str
    strategies: dict[str, Strategy]
    next_event_date: Callable[[], str]
    event_start: Callable[[str], datetime]
    previous_event_date: Callable[[str], str]
    pipeline_ready: Callable[[str], bool] = field(default=lambda _d: True)
    ops: dict[str, OpsJob] = field(default_factory=dict)
    snapshots: dict[str, Snapshot] = field(default_factory=dict)


def _tt_next_date() -> str:
    from .pipeline import next_tourn_date
    return next_tourn_date()


def _tt_start(tourn_date: str) -> datetime:
    return datetime.fromtimestamp(trading._expiry_ts_after_start(tourn_date, 0), timezone.utc)


def _tt_previous(tourn_date: str) -> str:
    d = datetime.strptime(tourn_date, '%Y-%m-%d').date() - timedelta(days=7)
    return d.isoformat()


def _tt_pipeline_ready(tourn_date: str) -> bool:
    """Predictions for `tourn_date` come from the prior week's completed run."""
    from .db import get_conn
    with get_conn() as conn:
        row = conn.execute('SELECT state FROM pipeline_runs WHERE tourn_date = ?',
                           (_tt_previous(tourn_date),)).fetchone()
    return bool(row and row[0] == 'completed')


_TT_STRATEGIES = {s.name: s for s in (
    Strategy(
        'place_bids', 'Place bids',
        'Resting bids on every market at max(fair / markup, fair − max discount), expiring 9 AM ET.',
        PlaceBidsParams,
        lambda client, d, p, emit: trading.plan_place_bids(client, d, emit=emit, **p),
    ),
    Strategy(
        'fade_no_bids', 'Fade NO bids',
        'Resting NO bids priced off P(top-N | plays), expiring shortly after tournament start.',
        FadeNoBidsParams,
        lambda client, d, p, emit: trading.plan_fade_no_bids(client, d, emit=emit, **p),
    ),
    Strategy(
        'take_trades', 'Take trades',
        'Fill-or-kill orders on live asks whose ROI clears the threshold.',
        TakeTradesParams,
        lambda client, d, p, emit: trading.plan_take_trades(client, d, emit=emit, **p),
        can_loop=True,
    ),
    Strategy(
        'cancel_bids', 'Cancel bids',
        'Cancels every resting order for this tournament.',
        CancelBidsParams,
        lambda client, d, p, emit: trading.plan_cancel_bids(client, d, emit=emit, **p),
        spends=False,
    ),
)}

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'


def _tt_recompute_command(tourn_date: str, params: dict) -> list[str]:
    return [sys.executable, str(_SCRIPTS / 'trigger_adjusted.py'), f'--date={tourn_date}']


def _tt_pipeline_command(tourn_date: str, params: dict) -> list[str]:
    cmd = [sys.executable, str(_SCRIPTS / 'weekly_pipeline.py')]
    if params.get('slug_url'):
        cmd.append(f'--slug-url={params["slug_url"]}')
    return cmd


def _tt_pipeline_error() -> str | None:
    """weekly_pipeline exits 0 after recording a failure, so check what it recorded."""
    from .db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            'SELECT state, error_message FROM pipeline_runs ORDER BY tourn_date DESC LIMIT 1'
        ).fetchone()
    if row and row[0] == 'failed':
        return row[1] or 'Pipeline failed'
    return None


MARKETS: dict[str, Market] = {
    'titled_tuesday': Market(
        slug='titled_tuesday',
        label='Titled Tuesday',
        strategies=_TT_STRATEGIES,
        next_event_date=_tt_next_date,
        event_start=_tt_start,
        previous_event_date=_tt_previous,
        pipeline_ready=_tt_pipeline_ready,
        ops={
            'recompute': OpsJob('recompute', 'Recompute predictions', _tt_recompute_command),
            'run_pipeline': OpsJob('run_pipeline', 'Run pipeline', _tt_pipeline_command,
                                   _tt_pipeline_error),
        },
        snapshots={
            'edges': Snapshot(market_views.tt_edges_snapshot, 15),
            'positions': Snapshot(market_views.tt_positions_snapshot, 30),
        },
    ),
}


class SubmissionError(ValueError):
    """The request was well-formed but can't be queued as given."""


def get_strategy(market: str, strategy: str) -> tuple[Market, Strategy]:
    try:
        m = MARKETS[market]
    except KeyError:
        raise SubmissionError(f'Unknown market {market!r}') from None
    try:
        return m, m.strategies[strategy]
    except KeyError:
        raise SubmissionError(f'Unknown strategy {strategy!r}') from None


def preview(client: Any, market: str, strategy: str, params: dict,
            tourn_date: str | None = None, emit: Emit | None = None) -> Plan:
    """Build a plan without sending anything."""
    from .trade_plan import noop_emit
    m, s = get_strategy(market, strategy)
    validated = s.params_model(**params).model_dump()
    return s.plan(client, tourn_date or m.next_event_date(), validated, emit or noop_emit)


def submit_ops(market: str, name: str, params: dict | None = None,
               created_by: str | None = None) -> int:
    """Queue a recompute or pipeline run. Refuses a duplicate of one already active."""
    try:
        m = MARKETS[market]
    except KeyError:
        raise SubmissionError(f'Unknown market {market!r}') from None
    if name not in m.ops:
        raise SubmissionError(f'Unknown job {name!r}')
    if job_queue.has_active_job(market, name):
        raise SubmissionError(f'{m.ops[name].label} is already queued or running')
    return job_queue.enqueue(market=market, strategy=name, tourn_date=m.next_event_date(),
                             params=params or {}, created_by=created_by)


DEFAULT_WINDOW_MINUTES = 15


def submit(
    *,
    market: str,
    strategy: str,
    params: dict,
    tourn_date: str | None = None,
    max_exposure: float | None = None,
    run_at: datetime | None = None,
    not_after: datetime | None = None,
    override_stale: bool = False,
    dry_run: bool = False,
    loop_interval_min: float | None = None,
    loop_fallback_hours: float | None = None,
    created_by: str | None = None,
) -> int:
    """Validate a trade request and add it to the queue.

    `max_exposure` is the approved dollar cap, checked against a fresh plan when
    the job fires. For a loop it is the total across all sweeps.

    Unless `not_after` is given, a one-shot must start within 15 minutes of its
    intended time, and a loop ends at the event start. A worker outage therefore
    can't fire a stale order hours late.
    """
    m, s = get_strategy(market, strategy)
    validated = s.params_model(**params).model_dump()
    now = job_queue.utcnow()

    if run_at is not None and run_at.tzinfo is None:
        raise SubmissionError('run_at must include a timezone')
    if not_after is not None and not_after.tzinfo is None:
        raise SubmissionError('not_after must include a timezone')
    if s.spends and not dry_run and (max_exposure is None or max_exposure <= 0):
        raise SubmissionError('A live trade needs a max exposure above $0')

    is_loop = loop_interval_min is not None
    if is_loop:
        if not s.can_loop:
            raise SubmissionError(f'{s.label} cannot run as a loop')
        if loop_interval_min <= 0 or not loop_fallback_hours or loop_fallback_hours <= 0:
            raise SubmissionError('A loop needs a positive interval and fallback wait')

    date = tourn_date or m.next_event_date()
    start_at = run_at or now
    if not_after is None:
        not_after = (m.event_start(date) if is_loop
                     else start_at + timedelta(minutes=DEFAULT_WINDOW_MINUTES))
    if not_after <= start_at:
        raise SubmissionError('The job would expire before it could start')

    return job_queue.enqueue(
        market=market, strategy=strategy, tourn_date=date, params=validated, dry_run=dry_run,
        run_at=start_at, not_after=not_after, max_exposure=max_exposure,
        override_stale=override_stale, loop_interval_min=loop_interval_min,
        loop_fallback_hours=loop_fallback_hours, created_by=created_by,
    )
