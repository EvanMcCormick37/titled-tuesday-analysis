Architecture proposal · Kalshi automated trading

# Reactive two-tier market making for Kalshi's Titled Tuesday markets

16 November 2026 · Draft v2 for review

**Contents**

1. [Summary](#summary)
2. [What the Kalshi API gives us](#constraints)
3. [System architecture](#architecture)
4. [Market data & book building](#marketdata)
5. [Fair value & the two-tier quoting logic](#strategy)
6. [Order management & execution](#oms)
7. [Risk management](#risk)
8. [Infrastructure, observability & testing](#infra)
9. [Phased roadmap](#roadmap)
10. [Open questions](#open)

## 1. Summary

The current bot quotes a fixed X% either side of an estimated market value. That leaves it passive: whenever another liquidity provider quotes even one tick better, that provider gets the taker flow. This proposal replaces it with an **event-driven system**. The system keeps a live copy of every Titled Tuesday order book over Kalshi's WebSocket feed and quotes in two layers per market and side:

| Layer | Purpose | Size | Price rule | Reprice frequency |
| --- | --- | --- | --- | --- |
| **Scout** (top-of-book) | Be first in the queue at the best price, so the bot gets the first taker fill | Linear cap: AV at the anchor price, tapering toward 0 at fair value (§5.4) | One tick above the next highest *competing* resting order, never inside a 5¢ buffer around fair value | On every relevant book change, within throttle limits |
| **Anchor** (deep) | Earn the full predetermined spread from larger takers who sweep through the book | AV (e.g. 200) | Fair value ± X% (today's logic) | On a new model run or a fallback shift |

The headline design decisions:

- **One writer to the exchange.** Strategy code never sends orders. It publishes a *desired quote set*, and a single Order Manager reconciles that against live orders, within a local copy of Kalshi's token-bucket rate limit.
- **The bot must not compete with itself.** Book changes caused by the bot's own orders are tagged with its `client_order_id`. The strategy works from an "ex-own" book, so it never pennies itself or mistakes its own order for a competitor.
- **A hard pre-start cutoff, enforced by the exchange.** Every order carries an expiration time 2 hours before the tournament starts, before registration opens. Kalshi then removes it even if the bot, the network or the watchdog has died. A cancel-all at the same moment backs this up.
- **Exchange-side circuit breakers.** Kalshi *order groups* auto-cancel a group of orders if more than N contracts fill within 15 seconds. This guards against bugs (a mispriced quote getting hit repeatedly) more than against informed flow.
- **Fair value comes straight from your PostgreSQL model store.** A new simulator run triggers a top-down reprice of every resting order for the event. A fully taken anchor sends you an alert and applies a bounded −10¢ fallback shift. It never shuts trading down.
- **Risk is measured per player, not per market.** A player's Winner, Top 3, Top 5, Top 8 and Top 10 contracts are highly correlated, so exposure is aggregated across them.
- **Record everything from day one.** Every WebSocket message and order action is written to an append-only journal. The same journal drives replay, backtesting and post-trade analysis.

**Scope: pre-tournament quoting only**The bot quotes only before the tournament starts, and every resting order expires 2 hours before the start. No live-round quoting. Your model's fair value is treated as the source of truth. Market prices tell the bot *where the competition is*, not *what the contract is worth*. That keeps the design simple: the scout can bid right up to fair value minus a fixed edge. The main risks become operational: a quote left resting after the start, rate-limit exhaustion, and pricing bugs. Section 5.5 and §7 are built around those.

## 2. What the Kalshi API gives us (design constraints)

### 2.1 Market data over WebSocket

- All data arrives over one authenticated connection, signed with the same API-key scheme as REST. Subscribing to `orderbook_delta` for a list of `market_tickers` returns an `orderbook_snapshot` first and then incremental `orderbook_delta` messages.
- Snapshots list YES and NO price levels as `[price_dollars, count_fp]` string pairs, for example `["0.0800","300.00"]`. Deltas carry `price_dollars`, `side` and a signed `delta_fp`.
- Each subscription has a `seq` number for detecting gaps. Each change also has an exchange `clock`, which is the same across connections, so it can be used to deduplicate redundant feeds. `get_snapshot` re-fetches a snapshot without unsubscribing.
- **Own-order attribution:** a delta includes `client_order_id` only when the bot's own order caused it. This is what makes an exact ex-own book possible.
- Other useful channels: `ticker`, `trade`, `fill`, `user_orders`, `market_position`, `order_group_updates`.

### 2.2 The book is bids-only (YES bids and NO bids)

Kalshi shows two ladders of *bids*. An "offer" to sell YES at price *p* appears as a **NO bid at 1 − p**. So "quoting both sides" of a contract means resting a YES bid and a NO bid:

```
best_yes_bid = max(yes_levels)
best_yes_ask = 1 − max(no_levels)        # implied by the best NO bid
our "offer" on YES at p  ⇔  NO bid at (1 − p)
```

All strategy code should work in a single YES-price frame and convert to NO at the Order Manager. That keeps the strategy logic symmetric and avoids sign errors. Prices are dollar strings with four decimal places. **Read each market's tick structure from the market metadata rather than assuming a 1¢ tick.**

### 2.3 Order types & endpoints we'll use

| Capability | Use in this design |
| --- | --- |
| Limit order with `post_only` | Every quote. A scout should never accidentally cross the book and pay taker fees. |
| `client_order_id` | Idempotency, plus attributing book changes and fills to a specific tier |
| Amend (price and/or count), Decrease | Cheaper repricing than cancel + new. *Verify* how amend affects queue priority before relying on it. |
| Batch create / batch cancel | Repricing many markets at once (for example, a full event after a model re-run) |
| Queue position endpoints | Measuring how often the scout is actually first in the queue (a diagnostic, not part of the hot path) |
| Order groups (contracts limit over a rolling 15 s window) | Exchange-side kill switch for the scout layer |
| `self_trade_prevention_type`, `cancel_order_on_pause`, expiration time | Safety settings on every order |
| Cancel all / mass cancel | Kill switch, and the first step on every startup |

### 2.4 Rate limits: the budget the whole design must fit

Kalshi uses token buckets. Each request costs tokens, and the bucket refills at the per-second budget for your tier. Above Basic, you can burst up to 3× the per-second budget. The default order costs 10 tokens:

| Tier | Write budget (tokens/s) | ≈ sustained orders/s at 10 tokens |
| --- | --- | --- |
| Basic | 100 | \~10 |
| Advanced | 300 | \~30 |
| Expert | 600 | \~60 |
| Premier | 1,200 | \~120 |

**Why this drives the design**A Titled Tuesday event lists around 50 player markets, and there are Winner, Top 3, Top 5, Top 8 and Top 10 events every week. That is a few hundred markets, each with up to 4 resting orders (2 sides × 2 tiers). On the Basic tier the bot cannot reprice everything on every book change. So the Order Manager needs a **prioritized, coalescing queue** (§6.3), and the scout layer should only run on the markets where taker flow actually happens. Check your actual numbers with `GET /account/limits`. Cancel and amend costs are listed per endpoint.

### 2.5 Fees

Resting orders and cancels are free. Makers pay nothing unless the series is listed with maker fees. In that case the maker formula is a quarter of the taker rate (0.0175·C·P·(1−P) versus 0.07·C·P·(1−P)), charged only on fills. On startup, read each series' `fee_type` (`quadratic` vs `quadratic_with_maker_fees`) and include it in the minimum-edge calculation. Free cancels make constant repricing economically viable. The rate limit, not cost, is the real constraint.

### 2.6 The Titled Tuesday markets themselves

- Series `KXTITLEDTUESDAY` (Winner) and `KXTITLEDTUESTOP` (Top 3/5/8/10). Example market ticker: `KXTITLEDTUESTOP-26JUN30T10-FCAR`. The format is event + top-N + player code.
- The event is an 11-round blitz Swiss starting at 11:00 ET. Markets open about a week before and close once the outcome is known. **Markets stay open during the event**, so the bot itself must stop quoting before the start. The exchange won't do it.
- Volume is thin, roughly a few thousand dollars per event. Expect wide books, few competitors, and occasional large taker sweeps. This is a good fit for the two-tier idea.

## 3. System architecture

Blue = hot path (book change → reprice). Orange dashed = feedback from the bot's own orders and fills. The Watchdog runs as a separate process with its own API key, so it can cancel everything even if the main process hangs.

### 3.1 Components

| Component | Responsibility | Notes |
| --- | --- | --- |
| Market Discovery | Every few minutes, list open events in the two series. Fetch market tickers, tick structure, fee type, and open/close times. Add or remove markets from the subscription live with `update_subscription`. | REST, low frequency |
| WS Gateway | Run one authenticated connection, plus an optional second one for redundancy (deduplicated by `clock`). Check `seq` on every message. On a gap, trigger `get_snapshot`. On disconnect, raise *BOOK_STALE*. | Hot path |
| Book Builder | Keep a price-level map for each market and side. Maintain the **full** book, and an **ex-own** book computed by subtracting the bot's own resting size at each level. | Hot path, pure function of messages |
| Fair Value Service | LISTEN for new model runs in PostgreSQL, validate them, and swap them in atomically. Apply fallback overrides. Own the event schedule and the T−2h cutoff clock (§5.1, §5.6). | Updates on model runs and anchor wipes |
| Alerter | Push anchor fills, fallback shifts, holds and cutoff confirmations to Telegram/Pushover, and accept simple reply commands | Also logged to an `alerts` table |
| Quoting Strategy | For each (market, side), compute the target scout and anchor quotes from the ex-own book, fair value and inventory. | Pure function → easy to test and replay |
| Risk Gate | Check every desired quote before it reaches the OMS. Can clamp, reject, or flatten everything. | Synchronous, cannot be bypassed |
| Order Manager | The only component that talks to REST. Diffs desired quotes against live orders, runs the order state machine, the token bucket and the priority queue. | Hot path |
| Position & P&L | Track positions in real time from the `fill` and `market_position` channels. Reconcile against REST every minute. |  |
| Event Journal | Append-only log of every inbound message and outbound request/response, with local monotonic timestamps | JSONL → Parquet nightly |
| Watchdog | Separate process. If the main process stops sending heartbeats, it calls cancel-all. | Its own API key |

### 3.2 Process model

Run a **single-threaded asyncio event loop** for the hot path (Python 3.12 + `uvloop` + `orjson`), split into in-process stages connected by queues. At 10–60 orders/s and a few hundred thin books, the network round-trip to Kalshi will dominate latency, not Python. A single loop also removes a whole category of race conditions between the book, strategy and OMS. Keep the Strategy and Book Builder as pure functions behind clean interfaces. That way, if profiling ever shows CPU is the bottleneck, they can be ported to Rust without touching the rest.

The **hot-path loop** for one book change:

```
WS msg → seq check → BookBuilder.apply() → mark market dirty
(coalesce: every ~25 ms, or immediately if the top-of-book changed)
  for each dirty market:
      desired = Strategy.quote(book_ex_own, fv, inventory, params)
      desired = RiskGate.check(desired)
      OMS.set_target(market, desired)       # non-blocking
OMS loop: diff targets vs live → prioritized action queue → token bucket → REST
```

## 4. Market data & book building

### 4.1 Book state

```
class Book:
    yes: SortedDict[price -> size]   # YES bids
    no:  SortedDict[price -> size]   # NO bids
    own: dict[(side, price) -> size] # our resting size at each level
    last_seq, last_clock, last_update_ms, stale: bool

    def best_yes_bid_ex_own(self):   # best bid that isn't ours
        for p, sz in reversed(self.yes.items()):
            if sz - self.own.get(("yes", p), 0) > 0: return p
    def best_yes_ask(self):          # implied by the NO book
        return 1 - max(self.no) if self.no else None
```

### 4.2 Keeping the ex-own book exact

Track the bot's own resting size per level from two sources. The Order Manager knows what it has acknowledged. Deltas tagged with `client_order_id` show when the bot's own orders added size, were filled, or were cancelled. Applying the tagged deltas to `own` keeps it in step with the exact order the exchange applied things. When the two sources disagree for more than a short window, a reconciler flags the market and pulls a REST snapshot of the bot's orders.

### 4.3 Gaps, staleness, reconnection

- **Seq gap** → mark the market *stale*, stop scout quoting on it, call `get_snapshot`, rebuild, then resume.
- **No messages for N seconds** on a market that should be active, or a failed WS ping → mark *all* markets stale.
- **Disconnect** → immediately cancel all *scout* orders. They're priced off a book the bot can no longer see. Anchors are priced off fair value, so they can stay up for a short grace period. Reconnect with exponential backoff and jitter, resubscribe, rebuild from snapshots, then resume.
- Never compare `clock` values from before and after an exchange maintenance window.

## 5. Fair value & the quoting logic

### 5.1 Fair value from your PostgreSQL model store

Your Monte Carlo simulator already writes its predictions to PostgreSQL, so the bot reads fair value (FP) straight from there. It never estimates value itself. Re-running the model is a **top-down event**: the simulator writes a new run, and the bot reprices every resting order for that event to match.

Suggested tables. Adapt the names to your existing schema; the important parts are a run ID and a ticker mapping:

```
model_runs   (run_id PK, event_key, created_at, status)          -- status: 'writing' → 'complete'
predictions  (run_id FK, player_id, contract_type, fp)            -- contract_type: WIN, TOP3, TOP5, TOP8, TOP10
ticker_map   (event_key, player_id, contract_type, market_ticker) -- e.g. KXTITLEDTUESTOP-26JUN30T10-FCAR
fp_overrides (market_ticker, delta, reason, created_at, cleared_at, cleared_by)   -- fallback shifts (§5.6)
-- bot-owned tables in the same DB: orders, fills, positions, alerts
```

Update flow:

1. The simulator writes all predictions for a run. As its last step it sets `status='complete'` and calls `NOTIFY fp_update, '<run_id>'`.
2. The bot holds a `LISTEN fp_update` connection. On a notification, it loads the whole run and validates it as a unit (checks below). If validation passes, it swaps the run in **atomically** for that event. The bot never quotes from a half-written run.
3. The strategy recomputes every target for the event. The OMS turns the differences into a batch of amends, with the largest price moves sent first (§6.3).
4. By default, a new run **clears that event's fallback overrides** (§5.6). Recalibrating the model counts as your judgement call superseding the automatic shift. This is configurable.

The bot also polls for new completed runs every 30–60 s, in case a notification is missed (for example, during a database reconnect).

Sanity checks on each run, there to catch data bugs, not to second-guess the model:

- Every FP is between 0 and 1.
- Every quoted market has an FP, and every FP maps to a live ticker.
- For each player: P(win) ≤ P(top 3) ≤ P(top 5) ≤ P(top 8) ≤ P(top 10).
- Winner probabilities sum to about 1, and Top-N probabilities sum to about N.

A failed check holds that event, keeps its current quotes or pulls them (configurable), and sends an alert.

### 5.2 Price bands per market and side

Everything is in the YES frame. The bid side is shown; the ask side mirrors it and physically rests as NO bids at 1 − price.

```
FP_eff = FP_model + Σ active overrides            # §5.6 fallback shifts
AP_bid = anchor price from today's X% logic        # below FP_eff
AP_ask = anchor price from today's X% logic        # above FP_eff
B      = 0.05                                      # safety buffer around fair value

scout bid band:  AP_bid  <  SP  ≤  FP_eff − B
scout ask band:  FP_eff + B  ≤  SP  <  AP_ask
```

A 5¢ buffer on each side means the bot's own best bid and best ask are always at least 10¢ apart. Each fill earns at least 5¢ of edge against fair value, which is 5% of the $1 payout.

**Low-probability players**When `FP_eff − B` is below one tick (for example a Winner market with FP = 4¢), there is no room for a YES-bid scout, so only the ask-side scout runs. The YES-bid anchor still rests if `AP_bid` is ≥ 1 tick. The same applies in reverse for heavy favourites in Top-10 markets.

### 5.3 Scout price: one tick above the next resting order

```
def scout_price_bid(book, FP_eff, AP_bid, B, tick):
    ceiling = min(FP_eff - B, book.best_yes_ask() - tick)   # never cross (post_only)
    b = book.best_yes_bid_ex_own()                          # best resting bid that isn't ours

    if b is None:            target = AP_bid + tick         # empty book: just inside our own anchor
    elif b + tick <= ceiling: target = b + tick             # IMPROVE by one tick → first in queue
    else:                    target = ceiling               # competitor is inside the buffer: rest at the buffer edge

    if target <= AP_bid:     return None                    # nothing to beat above the anchor: anchor is already best
    return target
```

The target is always computed from the best *competing* order, never from the bot's own current price. So stepping down happens automatically: if the competitor above the scout cancels, the next recompute drops the scout to one tick above whoever is next. The bot never leaves edge on the table by sitting alone far above the rest of the book.

The ex-own book (§4.2) matters here. Without it, the bot would see its own scout as "the next highest resting order" and keep outbidding itself up to the buffer edge.

### 5.4 Scout volume: a linear cap by distance from fair value

Your formula, `AV·((SP−FP)/(FP−AP))`, has the right shape but comes out **negative on both sides**. On the bid side SP \< FP, so the numerator is negative. On the ask side AP > FP, so the denominator is negative. Using distances fixes it, and the same expression then works for both sides:

```
scout_volume = floor( AV · |FP_eff − SP| / |FP_eff − AP| )
             → AV at SP = AP,   0 at SP = FP_eff,   linear in between
scout_volume = max(scout_volume, min_scout)        # e.g. 1, so a valid band never yields a 0-lot order
```

Worked example, with FP = 0.30, anchors at 0.20 / 0.40, AV = 200 and B = 0.05:

| Side | Scout price | Distance from FP | Scout volume |
| --- | --- | --- | --- |
| Bid | 0.21 | 9¢ | 180 |
| Bid | 0.23 | 7¢ | 140 |
| Bid | 0.25 (buffer edge) | 5¢ | 100 |
| Ask | 0.35 (buffer edge) | 5¢ | 100 |
| Ask | 0.38 | 8¢ | 160 |

**Two consequences worth a decision** 1. **With the 5¢ buffer, the scout is never small.** The closest it can get to FP is B, so the minimum scout size is `AV·B/|FP−AP|`, which is half the anchor in this example. If you want scouts to taper to zero at the buffer edge instead, measure the distance from `FP ± B`: `AV·|FP∓B − SP| / |FP∓B − AP|`. Both versions are a one-line config switch.\
2\. **Scout and anchor stack.** When a competitor sits just above your anchor, the resting volume on that side is about AV (scout) plus AV (anchor). If the intent is "never more than AV per side", set `anchor_volume = AV − scout_volume` while a scout is up. This proposal defaults to that, so a sweep through both levels fills at most AV per side.

### 5.5 Re-quoting rules

- **Hysteresis:** reprice only if the target moves by ≥ 1 tick and the triggering book state has lasted `min_dwell` (150–300 ms). Exception: being outbid triggers an immediate reprice.
- **Price-war governor:** if another bot outbids the scout tick-for-tick, both climb toward the buffer edge. Above N reprices per market per minute, switch to resting at the buffer edge for a cool-off period, and stop spending rate budget on the chase.
- **Volume follows price:** every reprice recomputes volume from §5.4. A price change uses amend (price and count together). A smaller volume at the same price uses decrease, which keeps queue position.
- **After a scout fill:** re-post at the newly computed price and volume. The fill doesn't move FP_eff, because your model is trusted. Only anchor wipes trigger the fallback (§5.6).

### 5.6 Anchor fills: alerts and the fallback shift

The bot **doesn't stop trading** because other people disagree with your attendance view. It keeps you informed, and applies a fixed, bounded fallback when an anchor is fully taken.

| Event | Bot action |
| --- | --- |
| Any anchor fill (partial or full) | Send a push alert with the details below. Quoting continues unchanged. |
| **Anchor wipe**: an anchor's remaining size reaches 0 on one side | Apply the fallback: shift FP_eff for that market by 10¢ *in the direction of the sweep*, write an `fp_overrides` row, recompute AP and scouts from the new FP_eff, re-post, and alert. |
| A second wipe on the same market | A second shift, up to `max_fallback_steps` (default 2). After that, hold that side and alert. This prevents a chain of −10¢ steps if someone keeps selling into you. |
| New model run for the event | Clear the overrides (default) and quote from the new FP. |

Direction of the shift: if **YES bids** are wiped, people are selling YES, which is the "player won't attend" case you described, so FP_eff moves **−10¢**. If the **YES ask** (NO-bid) anchor is wiped, the mirror is +10¢. I've applied the mirror by default. It's a single flag if you only want the downside version.

```
on_fill(f):
    if f.tier == ANCHOR:
        alert(f"{player} {contract} anchor {side} filled {f.qty} @ {f.price} | "
              f"remaining {anchor.remaining}/{AV} | pos {net_pos} | FP {FP_eff}")
        if anchor.remaining == 0 and steps[market, side] < max_fallback_steps:
            shift = -0.10 if side == BID else +0.10
            insert fp_overrides(market, shift, reason=f"anchor wipe {side}")
            FP_eff[market] = clamp(FP_eff[market] + shift, 0, 1)
            steps[market, side] += 1
            requote(market)                     # new AP, scouts, volumes
            alert(f"FALLBACK {player} {contract}: FP {old} → {FP_eff[market]}")
        elif anchor.remaining == 0:
            hold(market, side); alert("max fallback reached — holding side")
```

- **Scope of the shift:** by default it applies only to the market that was wiped. An attendance concern affects all of a player's markets (Winner, Top 3…Top 10), so `propagate_to_player=true` applies the same shift across that player's ladder. A flat −10¢ on a 6¢ Winner market just removes the bid, which is probably what you'd want in that case.
- **Alerts** go to Telegram or Pushover, and are stored in an `alerts` table. Optional replies to the alert bot (`/clear MARKET`, `/hold PLAYER`, `/resume PLAYER`) let you act from your phone without opening a terminal. Re-running the model remains the main way to respond.
- Overrides are stored in Postgres, so they survive a bot restart, and your simulator and dashboards can see them.

### 5.7 Quoting window: everything expires 2 hours before the start

`T_cut = tournament_start − 2h`. This stops all trading before registration begins, which also takes care of the late attendance-news risk. Since leaving an order resting past the cutoff is the one failure that really matters, it's enforced in four independent layers:

| Layer | Mechanism | Survives |
| --- | --- | --- |
| 1 · Order expiry | Every order is sent with an expiration time = `T_cut`, so Kalshi removes it automatically | Bot crash, network loss, watchdog failure |
| 2 · Strategy clock | After `T_cut`, the strategy emits an empty target set for the event, so the OMS cancels everything and refuses new orders | Normal operation |
| 3 · Cancel-all | At `T_cut`, an explicit batch cancel for the event, then a REST check that zero orders remain | OMS state drift |
| 4 · Watchdog | A separate process with its own copy of the schedule repeats the cancel-all at `T_cut` + 1 min and alerts if it finds any order | Main process hung |

`T_cut` comes from the market's published start time, not a hard-coded 11:00 ET. That way daylight-saving changes and rescheduled events can't shift it. Amended orders keep their original expiration. Verify that on demo, and if amend doesn't carry it, use cancel + create.

## 6. Order management & execution

### 6.1 Desired-state reconciliation

The strategy publishes `Target{market, side, tier, price, size}`. The OMS holds `Live{client_order_id, order_id, market, side, tier, price, remaining, state}` and repeatedly computes the smallest set of actions that turns Live into Target:

| Diff | Action |
| --- | --- |
| Target exists, no live order | Create (post_only, client_order_id = `{tier}-{market}-{side}-{uuid}`) |
| Price differs | Amend, or cancel + create if amend isn't suitable. Benchmark both on demo. |
| Only size is lower | Decrease (keeps queue position) |
| Only size is higher | Add a second order at the same price. Don't amend upward, which may lose queue priority. |
| Live order, no target | Cancel |

Because the OMS works on *targets* and not on commands, a burst of book changes for the same market collapses into one action. If the strategy changes its mind while a request is in flight, the next diff fixes it.

### 6.2 Order state machine

```
PENDING_NEW ─ack→ RESTING ─fill→ PARTIAL ─fill→ FILLED
     │              │  └─amend→ PENDING_AMEND ─ack→ RESTING
     │              └─cancel→ PENDING_CANCEL ─ack→ CANCELED
     └─reject→ REJECTED            (any) ─timeout→ UNKNOWN → reconcile via REST
```

- At most **one in-flight request per order**. Further changes wait in the target and are applied after the ack.
- Updates arrive from both REST responses and the `user_orders`/`fill` channels. Apply whichever arrives first, and make updates idempotent by checking the order's last-update time and fill count.
- **UNKNOWN** state (timeout or 5xx): don't retry the create blindly. Look it up by `client_order_id` first.

### 6.3 Throttling and prioritization

- Keep a **local token bucket** configured from `GET /account/limits` at startup, with a safety margin of about 80% of the budget. On a 429, use the server's view: back off and shrink the local budget.
- The **priority queue** orders work as follows:
  1. Risk-reducing cancels (kill switch, stale book, breached limits)
  2. Scouts that are now *outbid*, or inside the 5¢ buffer after an FP change
  3. Scout improvements in markets with recent trade activity
  4. Anchor reprices
  5. Quoting newly opened or low-activity markets
- **Coalesce** by (market, side, tier): a queued action is replaced by a newer one, never stacked behind it.
- Use **batch endpoints** when ≥ 3 actions are ready together, for example repricing a full event after an fv reload.
- Give each market a **fair share** of the budget, so one hot market can't starve the rest.
- **Top-down reprice cost:** a new model run can touch every order in an event. Roughly 50 markets × 4 orders = 200 amends takes about 20 s on Basic (\~10/s) and about 7 s on Advanced. Send the largest price moves first, and skip orders whose price and volume are unchanged after rounding to the tick.

### 6.4 Startup, shutdown, recovery

1. Startup: cancel all resting orders (or adopt them by `client_order_id` prefix), load positions from REST, create order groups, subscribe, wait for snapshots, then start quoting anchors first and scouts after.
2. Shutdown: stop the strategy, cancel all, wait for acks, flush the journal.
3. **Dead-man's switch:** the main process sends a heartbeat to the Watchdog every second. After 5 s of silence, the Watchdog cancels all *scout* orders, because they're priced off a book nobody is watching. Anchors are priced off FP alone, so they can stay up, but the fallback logic can't run while the main process is down. So the Watchdog alerts you, and the anchors' expiration at `T_cut` guarantees they're gone 2 hours before the start.

## 7. Risk management

### 7.1 Limits (pre-trade, in the Risk Gate)

| Limit | Scope | Example starting value |
| --- | --- | --- |
| Max order size | per tier | anchor = AV · scout = formula (§5.4) · scout + anchor ≤ AV per side |
| Price band | per order | scout never inside FP_eff ± 5¢; anchor within X% + a few ticks of FP_eff; no quotes in the 1–2¢ / 98–99¢ tails unless explicitly enabled |
| Net position | per market | ±200 contracts |
| Player exposure | across Winner/Top-N for one player | worst-case loss ≤ $X |
| Event max loss | per event | simulated worst case (or 99% CVaR) from the fair-value Monte Carlo ≤ $Y |
| Open-order exposure | account | Σ worst-case loss if every resting order fills ≤ $Z |
| Daily drawdown | account | realized + mark-to-fv ≤ −$D → flatten-and-halt |

The event-level measure reuses the tournament simulator. For each simulated finishing order, compute the portfolio's P&L across all markets and limit the tail. This automatically captures the fact that, for example, being long YES on five players in the Winner market is partly self-hedging.

### 7.2 Exchange-side protection

- **Order expiration ≤ `T_cut`** on every order (§5.7).
- **Order groups as a bug guard:** put each event's orders in a group with a generous `contracts_limit` per 15 s, comfortably above normal sweep sizes. It should only trigger if something is badly wrong, such as a mispriced fv set getting hit across many markets at once. Listen on `order_group_updates` and reset only after review.
- `post_only` on every order, self-trade prevention on, `cancel_order_on_pause` on.

### 7.3 Kill switches (automatic)

- Pre-start cutoff reached → cancel everything on that event (four layers, §5.7).
- Book stale (gap, disconnect, silence) → cancel scouts on the affected markets.
- Model run fails a sanity check (§5.1) → hold the affected event and alert.
- Fallback limit reached on a market (§5.6) → hold that side and alert.
- 429 rate or REST error rate above threshold → enter degraded mode: anchors only.
- Fill burst: more than N fills in M seconds on one event → cancel everything on that event and alert.
- Position/P&L breach → flatten-and-halt (optionally with IOC orders toward flat within a price band).
- Manual: a CLI or chat command that cancels everything and pauses the strategy.

### 7.4 What's left to worry about

With pre-start-only quoting and a trusted fv, most of the risk is operational rather than informational:

- **A resting order after the start**: the four-layer cutoff (§5.7).
- **Mapping errors**: the right FP on the wrong ticker (players with similar names, Top 5 vs Top 8). Caught by the `ticker_map` checks, and by reviewing a dry-run quote sheet before each event goes live.
- **Attendance disagreements** are a deliberate trading decision, not a risk control. Anchor-fill alerts tell you when someone is leaning hard on one player, the fallback limits how far the bot moves on its own, and you make the call by re-running the model.
- **Over-concentration**: winning the queue in many markets at once can build a large one-directional book. Player and event limits plus inventory skew cover this.
- **Rate-limit exhaustion** right before the cutoff, when the bot most needs to cancel: keep a reserved slice of the token budget for cancels only.

## 8. Infrastructure, observability & testing

### 8.1 Stack

| Concern | Choice | Why |
| --- | --- | --- |
| Language (v1) | Python 3.12, asyncio + uvloop, `websockets`/`aiohttp`, `orjson`, `sortedcontainers` | Fast to iterate on. Fast enough at these rate limits. |
| Auth | `cryptography` RSA-PSS signing, with keys loaded from a secrets manager or env, never from the repo | Same signing for REST and WS |
| Config | YAML per environment (demo/prod) + per-series parameters, hot-reloadable | Tune edge/size without restarting |
| Persistence | JSONL journal → Parquet nightly; SQLite/Postgres for orders, fills, positions, daily P&L | Replay + analytics |
| Metrics | Prometheus client + Grafana (or a simple hosted equivalent) |  |
| Alerts | Telegram / Pushover / Slack webhook | Kill-switch events, disconnects, limit breaches |
| Hosting | A small always-on cloud VM in a US-East region (measure round-trip from a couple of regions first), with systemd or Docker, auto-restart, and NTP | Stable latency; the Watchdog runs as a second service |
| Accounts | Consider a Kalshi subaccount for the bot, plus a separate API key for the Watchdog | Keeps the bot's positions and limits separate from manual trading |

### 8.2 Testing ladder

1. **Unit tests**: book builder (snapshot + deltas, gap → resync, ex-own maths, YES/NO conversion), strategy (join/improve/fade/step-down tables), OMS diffing and state machine.
2. **Replay simulator**: feed the journal back through the identical Book → Strategy → OMS code, with a simulated exchange that models queue position (FIFO behind existing size at the level) and taker trades from the `trade` channel. This gives estimated time-at-top, fill rate and markouts per parameter set.
3. **Demo environment**: the full system against `demo-api.kalshi.co`, to exercise the real API's acks, amends, order groups, 429s and reconnects.
4. **Shadow mode in production**: subscribe to live books and compute scout targets *without sending orders*. Log where the bot would have been and whether those prices traded. Run this for 2–3 Tuesdays.
5. **Small live**: anchors via the new OMS (replacing the old bot), then scouts on 5–10 of the most active markets, at minimum size.

### 8.3 What to measure

| Metric | Why it matters |
| --- | --- |
| Time at top of book (% of time scout is best bid/ask), by market | The direct test of the scout idea |
| Share of taker volume filled, by tier | Is being first actually capturing the flow? |
| Edge captured per fill (fill price vs FP) and realized P&L at settlement, by tier, plus P&L on markets where the fallback fired versus where it didn't | How much edge the scout gives up to win priority, versus how much extra volume it brings in. This is the scout's cost/benefit. |
| Model calibration (fv vs outcomes, over many events) | Fair value is trusted, so this is the main long-run check that it deserves to be |
| Spread captured vs fee paid | Profit per contract |
| Reprices per fill, tokens per fill, 429 count | Price-war and budget efficiency |
| Book-change → order-ack latency (p50/p99) | Hot-path health |
| Inventory by player / event over time | Is the skew working? |

## 9. Phased roadmap

| Phase | Scope | Exit criterion |
| --- | --- | --- |
| 0 · Capture | WS Gateway + Book Builder + Journal, recording every Titled Tuesday book | 2 full Tuesdays recorded with zero unexplained seq gaps |
| 1 · OMS on demo | Order Manager, Risk Gate, Watchdog, order groups, demo account | Survives forced disconnects, 429s and kill-switch drills with no orphaned orders |
| 2 · Anchors live | Port the current X%-spread logic onto the new OMS. Add inventory skew. | Same or better P&L than the old bot; position limits hold |
| 3 · Scout shadow | Scout logic runs on live books without sending orders; replay simulator calibrated | Simulated scout volume gain outweighs the edge it gives up |
| 4 · Scouts live (small) | 5–10 of the most active markets with a reduced AV, four-layer cutoff verified on each event, alerts and fallback tested on demo first | Scout fills add net P&L over 3+ events; zero orders found after `T_cut` |
| 5 · Scale | All markets in the series you trade, tuned `base_edge` and sizes, time-to-start parameter schedule | Per-tier P&L attribution shows each piece contributes |

## 10. Open questions

Settled since v1: pre-tournament only · cutoff at T−2h · FP from PostgreSQL · scout price and volume rules · anchor-fill alerts with a −10¢ fallback.

1. **Scout volume zero point (§5.4):** should it taper to 0 at FP (your formula, so the minimum scout is about half of AV with a 5¢ buffer) or at the buffer edge FP ± 5¢ (scouts get genuinely small near the buffer)?
2. **Stacking (§5.4):** confirm the cap is "scout + anchor ≤ AV per side", rather than the scout volume sitting on top of a full anchor.
3. **Fallback details (§5.6):** should a wipe shift only that market or the player's whole ladder? Should the +10¢ mirror apply on the ask side? Is a cap of 2 fallback steps right?
4. **Schema:** how do your current prediction tables identify a run, a player and a contract type? Is there already a player → Kalshi ticker mapping, or does the bot need to build one?
5. **API tier:** which Kalshi tier is the account on? This sets how fast a top-down reprice completes and how many markets get scouts.
6. **Market coverage:** Winner only, or the full Top-N ladder?
7. **Existing code:** what language is the current bot? The anchor (X%) logic can likely be reused behind the new OMS.
8. **Exchange checks on demo:** confirm how amend affects queue priority, and whether amended orders keep their expiration time.

## Sources

- [Kalshi docs: Orderbook updates (WebSocket)](https://docs.kalshi.com/websockets/orderbook-updates)
- [Kalshi docs: WebSocket connection](https://docs.kalshi.com/getting_started/websocket_connection)
- [Kalshi docs: Rate limits & tiers](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi docs: Order groups](https://docs.kalshi.com/getting_started/order_groups)
- [Kalshi docs: Amend order](https://docs.kalshi.com/api-reference/portfolio/amend-order)
- [Kalshi Help: Fees](https://help.kalshi.com/en/articles/13823805-fees)
- [QuantVPS: Kalshi fees explained (maker vs taker, fee_type)](https://www.quantvps.com/blog/kalshi-fees-explained)
- [Kalshi: Titled Tuesday Top 10 market (rules, tickers)](https://kalshi.com/markets/kxtitledtuestop/titled-tuesday-chess-top-finishers/kxtitledtuestop-26jun30t10)
- [Kalshi: Titled Tuesday category](https://kalshi.com/category/sports/chess/titled-tuesday)