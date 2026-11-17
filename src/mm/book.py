"""Order books built from Kalshi's orderbook_snapshot / orderbook_delta messages.

Kalshi books are bids only: a YES ladder and a NO ladder. Selling YES at p is a
NO bid at 100 − p. The market maker thinks in terms of "bid on side s", so the
price that would cross a bid on s is `100 − best bid on the other side`.

The book holds the full exchange view. Our own resting size is supplied by the
caller (the order manager knows what it has resting) to get the ex-own view.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

SIDES = ('yes', 'no')


def other(side: str) -> str:
    return 'no' if side == 'yes' else 'yes'


def _cents(price_dollars: str | float) -> int:
    return round(float(price_dollars) * 100)


@dataclass
class Book:
    ticker: str
    levels: dict[str, dict[int, float]] = field(default_factory=lambda: {'yes': {}, 'no': {}})
    stale: bool = True
    last_update: float = 0.0       # time.monotonic() of the last applied message

    def apply_snapshot(self, msg: dict) -> None:
        self.levels = {'yes': {}, 'no': {}}
        for side in SIDES:
            for price, qty in msg.get(f'{side}_dollars_fp') or []:
                q = float(qty)
                if q > 0:
                    self.levels[side][_cents(price)] = q
        self.stale = False
        self.last_update = time.monotonic()

    def apply_delta(self, msg: dict) -> int:
        """Apply one delta; returns the price level touched (cents)."""
        side = msg['side']
        price = _cents(msg['price_dollars'])
        q = self.levels[side].get(price, 0.0) + float(msg['delta_fp'])
        if q > 1e-9:
            self.levels[side][price] = q
        else:
            self.levels[side].pop(price, None)
        self.last_update = time.monotonic()
        return price

    def best_bid(self, side: str) -> int | None:
        lv = self.levels[side]
        return max(lv) if lv else None

    def best_bid_ex_own(self, side: str, own: dict[int, float]) -> int | None:
        """Best bid on `side` once our own resting size is removed level by level."""
        best = None
        for price, qty in self.levels[side].items():
            if qty - own.get(price, 0.0) > 1e-9 and (best is None or price > best):
                best = price
        return best

    def cross_price(self, side: str) -> int | None:
        """Lowest price at which a bid on `side` would trade (100 − best opposite bid)."""
        opp = self.best_bid(other(side))
        return None if opp is None else 100 - opp

    def top(self) -> tuple[int | None, int | None]:
        return self.best_bid('yes'), self.best_bid('no')
