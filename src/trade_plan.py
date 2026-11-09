"""Strategy-agnostic trade plans and the single code path that executes them.

Every strategy is split in two:

    plan_*(client, tourn_date, ...) -> Plan      # read-only; safe to preview
    execute_plan(client, plan, emit, ...)        # the only place orders are sent

Progress is reported through an `emit` callback rather than `print`, so the
same code drives the CLI (prints), the job worker (writes events to the DB) and
anything else that wants structured progress.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal, Protocol


class Emit(Protocol):
    def __call__(
        self,
        kind: str,
        message: str,
        *,
        level: str = 'info',
        ticker: str | None = None,
        data: dict | None = None,
    ) -> None: ...


def noop_emit(kind: str, message: str, *, level: str = 'info',
              ticker: str | None = None, data: dict | None = None) -> None:
    return None


def print_emit(kind: str, message: str, *, level: str = 'info',
               ticker: str | None = None, data: dict | None = None) -> None:
    """Emitter for the CLI scripts and notebook."""
    prefix = 'WARNING: ' if level == 'warning' else 'ERROR: ' if level == 'error' else ''
    print(f'  {prefix}{message}')


@dataclass
class OrderIntent:
    kind: Literal['place', 'take']
    ticker: str
    side: str                       # 'yes' | 'no'
    count: int
    price_c: int
    expiration_ts: int | None = None
    label: str = ''
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return self.count * self.price_c / 100.0


@dataclass
class CancelIntent:
    order_id: str
    ticker: str
    side: str = ''
    price_c: int | None = None
    qty: float | None = None


@dataclass
class Plan:
    strategy: str
    tourn_date: str
    orders: list[OrderIntent] = field(default_factory=list)
    cancels: list[CancelIntent] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)   # not serialised

    @property
    def exposure(self) -> float:
        """Gross dollars the new orders could spend, ignoring freed-up cancels."""
        return round(sum(o.cost for o in self.orders), 2)

    @property
    def action_count(self) -> int:
        return len(self.orders) + len(self.cancels)

    def to_dict(self) -> dict:
        return {
            'strategy':   self.strategy,
            'tourn_date': self.tourn_date,
            'exposure':   self.exposure,
            'orders':     [{**asdict(o), 'cost': round(o.cost, 2)} for o in self.orders],
            'cancels':    [asdict(c) for c in self.cancels],
            'skipped':    self.skipped,
            'unmatched':  self.unmatched,
            'stats':      self.stats,
        }


@dataclass
class ExecutionResult:
    placed: int = 0
    cancelled: int = 0
    failed: int = 0
    filled_exposure: float = 0.0     # takers that actually filled
    resting_exposure: float = 0.0    # resting bids Kalshi accepted
    stopped: bool = False

    @property
    def succeeded_any(self) -> bool:
        return self.placed > 0 or self.cancelled > 0

    def to_dict(self) -> dict:
        return asdict(self)


_DEAD_ORDER_STATUSES = {'canceled', 'cancelled', 'rejected', 'expired'}


def _take_was_killed(response: Any) -> bool:
    """True if Kalshi accepted a fill-or-kill request but reports it did not fill."""
    if not isinstance(response, dict):
        return False
    order = response.get('order') if isinstance(response.get('order'), dict) else response
    return str(order.get('status', '')).lower() in _DEAD_ORDER_STATUSES


def execute_plan(
    client: Any,
    plan: Plan,
    emit: Emit = noop_emit,
    should_stop: Callable[[], bool] = lambda: False,
    sleep_write: float = 0.10,
) -> ExecutionResult:
    """Send a plan to Kalshi. Per-order errors are recorded and do not abort.

    Order: cancels first, then resting orders (sequential, throttled, stoppable
    between orders), then takers (concurrent, because their edge is
    time-sensitive and fill-or-kill orders cannot be stopped once sent).
    """
    result = ExecutionResult()

    for c in plan.cancels:
        if should_stop():
            result.stopped = True
            emit('phase', 'Stop requested; halting before remaining orders.', level='warning')
            return result
        try:
            client.cancel_order(c.order_id)
            result.cancelled += 1
            emit('cancel', f'Cancelled {c.ticker} {c.side.upper()}'.rstrip(), ticker=c.ticker,
                 data={'ok': True, 'order_id': c.order_id})
        except Exception as e:  # noqa: BLE001 — one bad order must not abort the batch
            result.failed += 1
            emit('cancel', f'Could not cancel {c.order_id}: {e}', level='error',
                 ticker=c.ticker, data={'ok': False, 'order_id': c.order_id, 'error': str(e)})
        if sleep_write:
            time.sleep(sleep_write)

    for o in (o for o in plan.orders if o.kind == 'place'):
        if should_stop():
            result.stopped = True
            emit('phase', 'Stop requested; halting before remaining orders.', level='warning')
            return result
        desc = f'{o.ticker} {o.side.upper()} @ {o.price_c}¢ × {o.count}'
        try:
            client.place_order(ticker=o.ticker, side=o.side, count=o.count, price=o.price_c,
                               expiration_ts=o.expiration_ts, dry_run=False)
            result.placed += 1
            result.resting_exposure = round(result.resting_exposure + o.cost, 2)
            emit('order', f'Placed {desc}', ticker=o.ticker, data={'ok': True})
        except Exception as e:  # noqa: BLE001
            result.failed += 1
            emit('order', f'Could not place {desc}: {e}', level='error', ticker=o.ticker,
                 data={'ok': False, 'error': str(e)})
        if sleep_write:
            time.sleep(sleep_write)

    takes = [o for o in plan.orders if o.kind == 'take']
    if takes:
        if should_stop():
            result.stopped = True
            emit('phase', 'Stop requested; takers not sent.', level='warning')
            return result

        def _take(o: OrderIntent) -> tuple[OrderIntent, str | None]:
            try:
                resp = client.take_order(ticker=o.ticker, side=o.side, count=o.count,
                                         price=o.price_c, dry_run=False)
            except Exception as e:  # noqa: BLE001
                return o, str(e)
            return o, ('not filled (fill-or-kill)' if _take_was_killed(resp) else None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(takes)) as pool:
            outcomes = list(pool.map(_take, takes))

        for o, err in outcomes:
            desc = f'{o.ticker} {o.side.upper()} @ {o.price_c}¢ × {o.count}'
            if err is None:
                result.placed += 1
                result.filled_exposure += o.cost
                emit('order', f'Filled {desc}', ticker=o.ticker, data={'ok': True})
            else:
                result.failed += 1
                emit('order', f'Failed {desc}: {err}', level='error', ticker=o.ticker,
                     data={'ok': False, 'error': err})

    result.filled_exposure = round(result.filled_exposure, 2)
    return result


def format_plan(plan: Plan, dry_run: bool) -> str:
    """Human-readable plan for the CLI scripts and notebook."""
    tag = '[DRY RUN] ' if dry_run else ''
    lines = [f'\n{tag}{plan.strategy} plan for {plan.tourn_date}:']
    lines += [f'  {k.replace("_", " ").capitalize():18s}: {v}' for k, v in plan.stats.items()]
    lines += [
        f'  {"Orders":18s}: {len(plan.orders)} (exposure ${plan.exposure:.2f})',
        f'  {"Cancels":18s}: {len(plan.cancels)}',
        f'  {"Skipped":18s}: {len(plan.skipped)}',
        f'  {"Unmatched names":18s}: {len(plan.unmatched)}',
    ]
    if plan.unmatched:
        lines.append('\n  Unmatched player names:')
        lines += [f'    - {name!r}' for name in plan.unmatched[:20]]
        if len(plan.unmatched) > 20:
            lines.append(f'    ... (+{len(plan.unmatched) - 20} more)')
    if plan.skipped:
        lines.append('\n  Skipped:')
        lines += [f'    {s.get("ticker", "?")} — {s.get("reason", "")}' for s in plan.skipped[:20]]
        if len(plan.skipped) > 20:
            lines.append(f'    ... (+{len(plan.skipped) - 20} more)')
    if plan.cancels:
        lines.append('\n  Planned cancels:')
        lines += [f'    {c.ticker} {c.side.upper()} id={c.order_id}' for c in plan.cancels]
    if plan.orders:
        lines.append('\n  Planned orders:')
        lines += [f'    {o.ticker} {o.side.upper():3s} @ {o.price_c}¢ × {o.count}  {o.label}'.rstrip()
                  for o in plan.orders]
    if dry_run:
        lines.append('\n[DRY RUN] No orders placed or cancelled.')
    return '\n'.join(lines)
