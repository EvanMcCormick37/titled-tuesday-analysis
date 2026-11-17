"""Two-tier quoting: a pure function of book, fair value and parameters.

Every quote is a bid on one side of a contract (YES or NO), priced in that
side's own cents, so the logic is identical for both sides. A YES "offer" at p
is simply a NO bid at 100 − p, handled by quoting the NO side with fair 1 − FV.

    anchor  = today's place_bids price: max(fair / markup, fair − max_discount)
    scout   = one tick above the best *competing* bid, capped at fair − buffer
    sizes   = scout tapers linearly from AV at the anchor to 0 at fair;
              anchor gets what is left, so scout + anchor ≤ AV per side
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .book import Book
from .config import MMParams


@dataclass(frozen=True)
class Quote:
    price: int
    size: int


@dataclass(frozen=True)
class SideQuote:
    anchor: Quote | None
    scout: Quote | None
    note: str = ''


def side_fair(fair_yes: float, side: str, yes_factor: float = 1.0, no_factor: float = 1.0) -> float:
    """Fair value of a bid on `side`, after the player's fallback factors.

    A YES-side wipe scales the YES fair value down; a NO-side wipe scales the NO
    fair value down (equivalently, pushes YES up). Both can be active.
    """
    fy = fair_yes * yes_factor
    fn = (1.0 - fy) * no_factor
    return min(max(fy if side == 'yes' else fn, 0.0), 1.0)


def anchor_price(fair: float, p: MMParams, markup: float | None = None) -> int | None:
    """kalshi_core.bid_cents's rule, computed in cents with a float guard
    (bid_cents(0.70, 1.5, 15) floors 54.999… to 54)."""
    if fair <= 0:
        return None
    fair_c = fair * 100
    c = math.floor(max(fair_c / (markup or p.markup), fair_c - p.max_discount) + 1e-6)
    return c if 1 <= c <= 99 else None


def scout_volume(fair_c: float, scout_c: int, anchor_c: int, p: MMParams) -> int:
    span = fair_c - anchor_c
    if span <= 0:
        return p.anchor_volume
    vol = math.floor(p.anchor_volume * (fair_c - scout_c) / span)
    return max(p.min_scout, min(vol, p.anchor_volume))


def quote_side(
    book: Book,
    side: str,
    fair: float,
    own: dict[int, float],
    p: MMParams,
    *,
    markup: float | None = None,
    war_mode: bool = False,
    scouts: bool = True,
) -> SideQuote:
    fair_c = fair * 100
    cross = book.cross_price(side)

    anchor_c = anchor_price(fair, p, markup)
    if anchor_c is None:
        return SideQuote(None, None, 'fair maps to no valid bid')
    if cross is not None and anchor_c >= cross:
        anchor_c = cross - 1                      # never cross (post_only would reject)
    anchor_c = min(anchor_c, p.max_price_c)
    if anchor_c < p.min_price_c:
        return SideQuote(None, None, 'anchor below min price')

    scout: Quote | None = None
    note = ''
    if scouts and p.scouts_enabled and not book.stale:
        ceiling = math.floor(fair_c - p.buffer_c + 1e-9)
        if cross is not None:
            ceiling = min(ceiling, cross - 1)
        ceiling = min(ceiling, p.max_price_c)
        best = book.best_bid_ex_own(side, own)
        target = None
        if best is not None and best >= anchor_c:
            if war_mode:
                target, note = ceiling, 'price war: resting at buffer edge'
            elif best + 1 <= ceiling:
                target, note = best + 1, f'improve {best}¢'
            else:
                target, note = ceiling, f'competitor {best}¢ inside buffer'
        if target is not None and target > anchor_c:
            scout = Quote(target, scout_volume(fair_c, target, anchor_c, p))

    anchor_size = p.anchor_volume - (scout.size if scout else 0)
    anchor = Quote(anchor_c, anchor_size) if anchor_size > 0 else None
    return SideQuote(anchor, scout, note)
