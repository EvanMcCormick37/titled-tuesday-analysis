"""Risk gate: every desired quote passes through here before reaching the OMS.

Pure function over one market's full target set, so limits that span markets
(a player's Winner/Top-N ladder, the whole event) see everything at once.

    position     a bid on YES adds long-YES exposure: size ≤ max_position − position
                 (position is signed, YES positive); a NO bid mirrors it
    player       resting $ per player per side ≤ max_player_notional (sizes scaled down)
    event        resting $ for the event ≤ max_event_notional (sizes scaled down)

Scouts are trimmed before anchors when a position limit binds, so the deep
anchor stays as long as possible.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .config import MMParams
from .oms import ANCHOR, SCOUT, Slot
from .quoting import Quote


def _notional(q: Quote) -> float:
    return q.price * q.size / 100


def _scale(targets: dict[Slot, Quote | None], slots: list[Slot], factor: float) -> None:
    for s in slots:
        q = targets[s]
        if q is not None:
            size = math.floor(q.size * factor)
            targets[s] = Quote(q.price, size) if size >= 1 else None


def check(
    targets: dict[Slot, Quote | None],
    positions: dict[str, float],
    player_of: dict[str, str],
    p: MMParams,
) -> dict[Slot, Quote | None]:
    out = dict(targets)

    # Position headroom per (ticker, side): anchors keep priority over scouts.
    for ticker in {s[0] for s in out}:
        pos = positions.get(ticker, 0.0)
        for side in ('yes', 'no'):
            room = p.max_position - (pos if side == 'yes' else -pos)
            for tier in (ANCHOR, SCOUT):
                slot = (ticker, side, tier)
                q = out.get(slot)
                if q is None:
                    continue
                size = int(min(q.size, max(room, 0)))
                out[slot] = Quote(q.price, size) if size >= 1 else None
                room -= size

    # Player notional per side.
    by_player_side: dict[tuple[str, str], list[Slot]] = defaultdict(list)
    for s in out:
        by_player_side[(player_of.get(s[0], s[0]), s[1])].append(s)
    for slots in by_player_side.values():
        total = sum(_notional(out[s]) for s in slots if out[s] is not None)
        if total > p.max_player_notional:
            _scale(out, slots, p.max_player_notional / total)

    # Event notional.
    total = sum(_notional(q) for q in out.values() if q is not None)
    if total > p.max_event_notional:
        _scale(out, list(out), p.max_event_notional / total)
    return out
