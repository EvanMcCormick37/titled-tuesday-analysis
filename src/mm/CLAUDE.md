# Market maker (`src/mm/`, service `tt-market-maker`)

Replaces resting `place_bids` with an event-driven quoter. One asyncio process quotes every registered market over one Kalshi WebSocket and one shared token bucket (the account is on the Basic tier: 100 write tokens/s; creates and amends cost 10, cancels 2). Entry point: `scripts/market_maker.py`. Shadow mode unless `MM_LIVE=1`.

## Premises (don't relitigate without the user)

- **The model's fair value is trusted.** Market prices show where the competition is, not what a contract is worth. Being sold into is a disagreement about attendance, handled by the bounded wipe fallback and alerts, never by stopping trading.
- **Kalshi's book is bids only.** An offer to sell YES at p is a NO bid at 100 − p, so the best YES ask = 100 − best NO bid. Quoting both sides means a YES bid and a NO bid.
- **Kalshi keeps these markets open during the event.** The exchange never stops our quotes, which is why the cutoff has four layers.
- **Rate limit, not cost, is the constraint.** Resting and cancelling are free; maker fees apply only on series listed with them (0.0175 · C · P · (1 − P), versus 0.07 for takers; `kalshi_core.kalshi_order_price`).

## Quoting

- Two tiers per contract and side (YES bid, NO bid), all prices integer cents in that side's frame. **Anchor** = the old place_bids price, `max(fair/markup, fair − max_discount)`. **Scout** = one cent above the best *competing* bid, capped at `fair − buffer_c` (default 2¢) and below the cross. Below 45¢ fair the anchor is fair / markup, so the ceiling only clears it above 3 × buffer: no scout on contracts at or below 6¢ fair. Scout size tapers linearly from AV at the anchor to 0 at fair; anchor size = AV − scout. Pure function: `quoting.quote_side`.
- **Exit layer** (`exits_enabled`, default **off**): when switched on, rests a sell of every position at fair value (a long YES is offered as a NO bid at 100 − fair), `client_order_id` prefix `mmx-`. Turned off on 2026-11-25: selling at fair gets adversely selected (the holdings that sell are the underpriced ones, the overpriced ones stay) and gives up edge, which the risk reduction no longer justifies. Switching it off cancels resting `mmx-` orders on the next cycle.
- The ex-own book subtracts **all** of the account's resting orders (MM, place_bids, fade, manual), so the scout never outbids us.
- The price-war governor rests at the buffer edge silently (no alert).
- Quoting is active only when the market is unpaused, not held (a model run that fails hard validation holds the market and raises a `model_invalid` alert), fair values are loaded, and the time is before the cutoff (minus 5 s).

## Fair value and fallbacks

- Fair value: `latest_model_predictions` (`p_participate × P_top{N}_given_play`), used only when the prior event's `pipeline_runs` row is `completed` and no recompute is running; validated as a unit (`fair_value.validate`). A new version reprices everything and clears fallback factors.
- Anchor wipe (an anchor fully filled, at least `wipe_min_qty`): the player's fair value on that side is multiplied by `1 − wipe_ratio` across **all** their markets (`mm_player_overrides`). `max_wipes` wipes pull the player until **Repost** on the Market maker page (optionally with a per-player markup). New model runs clear factors, not pulls.

## Orders, cutoff and failure handling

- Orders: `post_only`, `client_order_id` prefixed `mma-`/`mms-`/`mmx-` (the MM, its startup cancel and the watchdog only touch those), expiration = cutoff (`cutoff_minutes_before_start`, default 120), one order group per market. Price changes amend only if never filled, else cancel + recreate (amend count semantics after fills are ambiguous). Amending price loses queue position; decreasing keeps it.
- Cutoff in four layers: order expiry, strategy clock, tt-mm cancel-all + verify, tt-worker watchdog (+1 min). Watchdog also cancels scouts if tt-mm's heartbeat is >20 s old.
- **Stale books:** a WebSocket drop or an `orderbook_delta` seq gap marks every book stale. Scouts come down (they're priced off a book we can't see) and anchors stay (priced off fair value alone). A gap triggers `get_snapshot`; a drop reconnects with backoff and resubscribes.

## Control, storage, API

- Control: Market maker page (pause/resume, parameters stored as JSON overrides in `mm_control.params` and hot-reloaded, player repost/pull, alerts, fills, live quote sheet). The kill switch also pauses the MM. `place_bids` is legacy and refused while the MM is live and unpaused on that market.
- Parameters and their defaults: `MMParams` in `src/mm/config.py` (per-market overrides validated by pydantic, `extra='forbid'`).
- Tables (`mm_control`, `mm_player_overrides`, `mm_alerts`, `mm_status`, `mm_fills`) are created by `job_queue.init_schema()`, so no migration is needed.
- Journal: JSONL of every WS message and action, one file per UTC day in `MM_JOURNAL_DIR`.
- API: `GET /api/markets/{slug}/mm` (status, quotes, alerts, fills), `POST …/mm/pause`, `PUT …/mm/params`, `POST …/mm/players/{username}/repost|pull`, `POST …/mm/alerts/ack-all`, `POST /api/mm/alerts/{id}/ack`.

## Modules and tests

- `config` (`MMParams`, order-id prefixes), `book` (WS books + ex-own view), `quoting` (pure pricing/sizing), `fair_value`, `risk` (position/player/event limits), `oms` (target diffing, token bucket, order states), `gateway` (`LiveGateway` / `ShadowGateway`), `engine` (the asyncio process), `watchdog` (run by tt-worker), `store` (`mm_*` tables), `journal`.
- **kalshi-core has generalised copies** of `book`, `oms`, `gateway` and the fee maths (`kalshi_core.Book`, `OMS`, `LiveGateway`, `cancel_in_chunks`…). This package still uses its own. When you fix a bug in one, check the other.
- Tests: `tests/test_mm_core.py` (quoting, book, risk, OMS) and `tests/test_mm_engine.py` (engine with a fake gateway).
