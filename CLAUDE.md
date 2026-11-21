# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@../kalshi-core/CLAUDE.md

## Project Purpose

Production Python trading system for betting on chess.com's weekly **Titled Tuesday** blitz tournament and **Bullet Brawl** 1+0 arena using Kalshi prediction markets. The system:
1. Scrapes historical standings from chess.com + concurrent-event broadcast data from Lichess
2. Predicts each player's probability of attending and finishing in the top N (Monte Carlo)
3. Places and executes orders on Kalshi using those fair-value estimates

As of 2026-10, the system is deployed on **Railway** across several services sharing one Docker image + one Postgres instance. Local development against SQLite still works unchanged via the dual-backend DB layer.

## Deployment (Railway)

Services + one Postgres plugin, all sharing the single `Dockerfile` at the repo root:

| Service | Start command | Purpose |
|---|---|---|
| `tt-cron` | `python scripts/weekly_pipeline.py` | Titled Tuesday weekly scrape → broadcasts refresh → raw MC. Cron: `0 19 * * 2` (UTC — 15:00 EDT / 14:00 EST) |
| `bb-cron` | `python scripts/weekly_pipeline.py --market bullet_brawl` | The same pipeline for Bullet Brawl. Cron: `0 20 * * 6` (UTC — Saturday 16:00 EDT / 15:00 EST) |
| `tt-app` | `python -m streamlit run app/dashboard.py --server.port=$PORT --server.address=0.0.0.0` | Mobile-friendly Streamlit dashboard — primary user interface |
| `tt-api` | `/bin/sh -c "exec python -m uvicorn api.main:app --host 0.0.0.0 --port $PORT"` | FastAPI + built React dashboard (`web/`). Replaces `tt-app` at cutover |
| `tt-worker` | `python scripts/trade_worker.py` | Runs the trade queue, recomputes, pipeline retries, attendance research, live Kalshi snapshots, and the market-maker watchdog. Run exactly one replica; restart policy ON_FAILURE |
| `tt-market-maker` | `python scripts/market_maker.py` | Reactive two-tier market maker for every registered market (see "Market maker" below). Shadow unless `MM_LIVE=1`. Run exactly one replica; restart policy ON_FAILURE. Called `tt-mm` in `railway.toml` and the code; tt-worker finds it by its DB heartbeat, so the service name doesn't matter |

**Important Railway gotcha:** Cron expressions are evaluated in UTC regardless of the `TZ` env var. Use numeric day-of-week (`2` = Tuesday, `6` = Saturday), not the `TUE` alias. `TZ=America/New_York` only affects the container's clock inside Python (which matters for `MarketSpec.most_recent_event()`, the date each pipeline run is for).

Env vars (set in Railway UI, per service unless noted):
- **Shared:** `DATABASE_URL` (always the reference `${{Postgres.DATABASE_URL}}`, never a pasted URL: a copied public URL goes stale when the password changes, which crashed tt-worker and tt-cron in 2026-11), `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_B64` (base64 of `~/.kalshi/trading-key.txt`), `KALSHI_ENV=prod`, `TZ=America/New_York`, `GH_TOKEN` (PAT for installing private `kalshi-core`)
- **tt-app only:** `DASHBOARD_PASSWORD`
- **tt-worker only:** `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30`
- **tt-market-maker only:** `MM_LIVE=1` to send orders (otherwise shadow), optional `MM_SCOUTS=0` (anchors only), `MM_MARKETS` (comma-separated slugs), `MM_JOURNAL_DIR` (default `data/mm_journal`; mount a volume there to keep journals across deploys). Needs a kalshi-core build that includes `KalshiWS`, so push kalshi-core `main` before deploying

**Schema changes are not applied on deploy.** Before deploying code that needs new columns or tables, run the migration against Railway, e.g. `railway ssh` into tt-worker and run `python scripts/migrate_multi_market.py` (it uses the internal `DATABASE_URL`), or from your machine with `DATABASE_URL='<public URL>' python scripts/...` set for that one command. Only the queue tables (`job_queue.init_schema()`) are created automatically, by tt-api and tt-worker at startup.

The `Dockerfile` installs `kalshi-core` at build time from a private GitHub repo using `GH_TOKEN`. Local dev can bind-mount `/opt/kalshi-core` instead — `docker-entrypoint.sh` handles both paths.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# ── Local one-off tasks ─────────────────────────────────────────────────────

# Full weekly pipeline (scrape → broadcasts → raw MC → adjustments_pending)
python scripts/weekly_pipeline.py
python scripts/weekly_pipeline.py --market bullet_brawl

# Just scrape new TT standings
python scripts/scraping/update_titled_tuesday.py

# Just scrape new Bullet Brawl standings (or pass an event URL; --enrich-only fills in player names)
python scripts/scraping/update_bullet_brawl.py

# Weekly broadcast refresh (Lichess PGN dumps + attendance_conflicts rebuild)
python scripts/scraping/update_broadcasts.py

# Re-run `run_adjusted` against the current manual_adjustments table
python scripts/trigger_adjusted.py
python scripts/trigger_adjusted.py --market bullet_brawl

# Walk-forward joint backtest of score vs perf-rating MC (writes to `backtest` table)
python scripts/run_backtest.py

# ── Trading (CLI — all auto-resolve tourn_date via next_tourn_date()) ────────

# Place resting bids on Kalshi (dry run by default; pass --live to go live)
python scripts/place_bids.py

# Place fade NO bids expiring N minutes after 11:00 ET tournament start
python scripts/place_fade_bids.py

# Fire taker orders on high-ROI opportunities (optional --loop)
python scripts/take_trades.py

# Cancel all resting TT orders
python scripts/cancel_bids.py

# ── Dashboard (local dev) ───────────────────────────────────────────────────

python -m uvicorn api.main:app --reload     # React dashboard API (all markets)
python -m streamlit run app/dashboard.py    # legacy Streamlit app (Titled Tuesday only)

# ── Market maker ────────────────────────────────────────────────────────────
python scripts/market_maker.py              # shadow: live books, computes quotes, sends nothing
MM_LIVE=1 python scripts/market_maker.py    # live
```

The trading CLI scripts above cover Titled Tuesday only; trade Bullet Brawl from the React dashboard.

The `notebooks/command-center.ipynb` remains the richest interactive control surface for exploratory analysis, though for day-to-day production use the Streamlit dashboard has superseded it.

## Architecture Overview

### Data Flow

```
Chess.com ─┐
Lichess   ─┤→ scrapers → Postgres (Railway) or SQLite (local)
           │                 ↓
           │        src/data.py (load_and_prepare)
           │                 ↓
           │        src/simulation.py (100k Monte Carlo)
           │                 ↓
           │        latest_model_predictions_raw ──┐
           │                                        │  src/pipeline.run_adjusted
           │        manual_adjustments ────────────┤      (merges adjustments
           │                                        │       into raw sim)
           │                                        ↓
           │                                latest_model_predictions
           │                                        ↓
           │              ┌─────────────────────────┴──────────────────────────┐
           │              ↓                                                    ↓
           │       api/main.py + web/                                   src/trading.py
           │       (React dashboard)                                    (place / take / cancel / fade)
           │              │                                                    ↓
           └─ pipeline_runs                                              kalshi_core.KalshiClient
              (state machine for                                               ↓
              the whole weekly cycle)                                      Kalshi API
```

### Entry points (where to start reading)

- **`scripts/weekly_pipeline.py`** — orchestrates a market's three pre-trade steps (`MarketSpec.pipeline_steps`: `scrape_tt` or `scrape_bb` → `update_broadcasts → run_raw`) as a resumable state machine. Writes to `pipeline_runs` + `job_runs`. On any failure, halts with `state='failed'`, `error_kind` classified for the dashboard to show an appropriate retry UI.
- **`scripts/trigger_adjusted.py`** — `run_adjusted` entry point. Reads `manual_adjustments` for the upcoming tourn_date, re-simulates, writes `latest_model_predictions`, flips the matching `pipeline_runs` row to `completed`.
- **`api/main.py`** + **`web/`** — FastAPI + React dashboard for every registered market (market picker in the top bar). Pages: Trade / Market maker / Edges / Portfolio / Odds / Attendance / Pipeline / Logs. Gated by `DASHBOARD_PASSWORD`.
- **`scripts/market_maker.py`** — tt-market-maker entry point; runs `src/mm/engine.py`.
- **`app/dashboard.py`** — legacy Streamlit entry, Titled Tuesday only. Seven tabs: Odds / Attendance / Pipeline / Portfolio / Trade / Edges / Jobs.

### Trade queue

All dashboard trading goes through one Postgres-backed queue. `tt-api` only adds jobs; `tt-worker` is the only process that sends them.

- **`src/trade_plan.py`** — `Plan`, and `execute_plan()`, the single code path that sends orders. Progress goes through an `emit` callback, not `print`.
- **`src/trading.py`** — read-only `plan_*` functions; the older `place_bids` etc. are print-based wrappers for the CLI and notebook.
- **`src/strategies.py`** — registry of markets, strategies, param schemas, ops jobs, and snapshots. Add a market here. Forms in the UI are generated from the param schemas.
- **`src/job_queue.py`** — `trade_jobs`, `trade_job_events`, `market_snapshots`. Created automatically by `init_schema()` on API and worker start.
- **`src/job_runner.py`** — `JobRunner` and `Worker`.

Rules: jobs **re-plan when they fire**, and a plan above the approved `max_exposure` halts as `needs_review`. Spending strategies also halt if the prior week's `pipeline_runs` row isn't `completed`. One normal-lane job runs per market (enforced by a partial unique index), and recomputes share that lane because they briefly empty the predictions table. `background`-lane jobs (attendance research) run alongside and never block it. The kill switch (`enqueue_kill`) uses the priority lane. It also pauses the market maker for that market, then waits for tt-mm to confirm the pause (`paused_ack`) before sweeping orders; if no confirmation arrives in time it sweeps anyway and sweeps again afterwards. Live jobs are never auto-retried: a job whose worker dies becomes `interrupted`. Unless given `not_after`, a one-shot expires 15 minutes after its intended start and a loop ends at event start. For a loop, `max_exposure` is the total budget.

Tests: `python -m pytest tests` (fake Kalshi client; set `TEST_PG_URL` to a disposable Postgres to also run against Postgres — tables are dropped). Frontend: `cd web && npm run dev` with the API on port 8000. `DASHBOARD_PASSWORD` is required (login fails closed); set `TT_INSECURE_COOKIE=1` for plain-http local testing.

### Market maker (`src/mm/`, service `tt-market-maker`)

Replaces resting `place_bids` with an event-driven quoter (design: `Kalshi Market-Making Architecture — Titled Tuesday.md` at the repo root). One asyncio process quotes every registered market over one Kalshi WebSocket and one shared token bucket (the account is on the Basic tier: 100 write tokens/s; creates and amends cost 10, cancels 2).

- Two tiers per contract and side (YES bid, NO bid), all prices integer cents in that side's frame. **Anchor** = the old place_bids price, `max(fair/markup, fair − max_discount)`. **Scout** = one cent above the best *competing* bid, capped at `fair − buffer_c` (default 2¢) and below the cross. Below 45¢ fair the anchor is fair / markup, so the ceiling only clears it above 3 × buffer: no scout on contracts at or below 6¢ fair. `buffer_c` is also the attendance researcher's halt threshold. Scout size tapers linearly from AV at the anchor to 0 at fair; anchor size = AV − scout. Pure function: `quoting.quote_side`.
- The ex-own book subtracts **all** of the account's resting orders (MM, place_bids, fade, manual), so the scout never outbids us.
- Fair value: `latest_model_predictions` (`p_participate × P_top{N}_given_play`), used only when the prior event's `pipeline_runs` row is `completed` and no recompute is running; validated as a unit (`fair_value.validate`). A new version reprices everything and clears fallback factors.
- Anchor wipe (an anchor fully filled, at least `wipe_min_qty`): the player's fair value on that side is multiplied by `1 − wipe_ratio` across **all** their markets (`mm_player_overrides`). `max_wipes` wipes pull the player until **Repost** on the Market maker page (optionally with a per-player markup). New model runs clear factors, not pulls.
- Orders: `post_only`, `client_order_id` prefixed `mma-`/`mms-` (the MM, its startup cancel and the watchdog only touch those), expiration = cutoff (`cutoff_minutes_before_start`, default 120), one order group per market. Price changes amend only if never filled, else cancel + recreate (amend count semantics after fills are ambiguous). Amending price loses queue position; decreasing keeps it.
- Cutoff in four layers: order expiry, strategy clock, tt-mm cancel-all + verify, tt-worker watchdog (+1 min). Watchdog also cancels scouts if tt-mm's heartbeat is >20 s old.
- Control: Market maker page (pause/resume, parameters stored as JSON overrides in `mm_control.params` and hot-reloaded, player repost/pull, alerts, fills, live quote sheet). The kill switch also pauses the MM. `place_bids` is legacy and refused while the MM is live and unpaused on that market.
- Tables (`mm_control`, `mm_player_overrides`, `mm_alerts`, `mm_status`, `mm_fills`) are created by `job_queue.init_schema()`, so no migration is needed.
- Journal: JSONL of every WS message and action, one file per UTC day in `MM_JOURNAL_DIR`.
- Quoting is active only when the market is unpaused, not held (a model run that fails hard validation holds the market and raises a `model_invalid` alert), fair values are loaded, and the time is before the cutoff (minus 5 s).
- Modules: `config` (`MMParams`, order-id prefixes), `book` (WS books + ex-own view), `quoting` (pure pricing/sizing), `fair_value`, `risk` (position/player/event limits), `oms` (target diffing, token bucket, order states), `gateway` (`LiveGateway` / `ShadowGateway`), `engine` (the asyncio process), `watchdog` (run by tt-worker), `store` (`mm_*` tables), `journal`.
- API: `GET /api/markets/{slug}/mm` (status, quotes, alerts, fills), `POST …/mm/pause`, `PUT …/mm/params`, `POST …/mm/players/{username}/repost|pull`, `POST /api/mm/alerts/{id}/ack`.
- Tests: `tests/test_mm_core.py` (quoting, book, risk, OMS) and `tests/test_mm_engine.py` (engine with a fake gateway).

**Parameters** (`MMParams` in `src/mm/config.py`; per-market overrides from the Market maker page, validated by pydantic, `extra='forbid'`). Defaults:

| Group | Param | Default |
|---|---|---|
| Anchor | `anchor_volume` (contracts per side), `markup`, `max_discount` (¢) | 200, 1.5, 15 |
| Scout | `scouts_enabled`, `buffer_c`, `min_scout`, `min_dwell_ms` | true, 2, 1, 200 |
| Price war | `war_max_reprices` (per min), `war_cooloff_s` (rests at the buffer edge meanwhile) | 8, 120 |
| Wipes | `wipe_ratio`, `wipe_min_qty`, `max_wipes`, `wipe_mirror` | 0.33, 10, 2, true |
| Risk | `max_position` (per market), `max_player_notional` ($ resting per player per side), `max_event_notional` ($), `min_price_c`, `max_price_c`, `order_group_limit` (contracts / 15 s) | 400, 400, 5000, 2, 97, 2000 |
| Timing | `cutoff_minutes_before_start` | 120 |

**Going live:** run in shadow first and check the Market maker page (mode `shadow`, fresh heartbeat, WS connected, no `fv_errors`, sensible quote sheet). Lower the size and risk limits for the first live event, optionally with scouts off. Then set `MM_LIVE=1` on the service (it redeploys). On startup it cancels its own `mma-`/`mms-` orders and begins quoting. tt-worker must be running for the watchdog. To stop, use **Pause** or the kill switch; to return to shadow, unset `MM_LIVE`.
- kalshi-core gained `KalshiWS`, `order_body`/`create_order`, `amend_order`, `decrease_order`, `batch_create_orders`, `batch_cancel_orders`, `create_order_group`, `get_account_limits`.

### Attendance researcher (`src/research/`)

Finds other tournaments that may keep high-value players away from the next event. Design and roadmap: `Attendance Researcher Architecture — Proposal.md` at the repo root. **Phase 1 (built) is deterministic, with no LLM.** Phases 2–4 add the LLM investigation (minirouter.sh, `zai/glm-5.3-flash`), proposals with accept/edit/reject, scheduling, and the emergency halt.

- Runs as the `research` ops job in the **`background` lane**: a subprocess on tt-worker (`scripts/research_attendance.py --market --date`) that never holds the market's trade lane, because it doesn't change predictions or orders. Started from **Run now** on the Attendance page (no schedule yet). The kill switch leaves it alone.
- **Candidates:** players whose raw-model fair value `p_participate × P_top8_given_play` is above 5% (`candidate_min_fv`, `candidate_n`), one row per player.
- **Calendar:** the Lichess broadcast API (`/api/broadcast/top` + `/api/broadcast`, every tier, then rounds and `/broadcast/{id}/players` for events within a day of the event), plus events you add on the Attendance page. chess.com/events can't be read automatically (Cloudflare challenges headless browsers, tested 2026-11-20); add its events by hand. Don't add stealth or challenge-solving.
- **Clash test** (`overlap.py`): window = start to start + `MarketSpec.event_duration_minutes` (TT 150, BB 120). A round overlapping the window is `hard`, within `adjacent_hours` is `adjacent`, within `mid_event_hours` is `multi_day`. Round length depends on time control (classical 300 min). Season-long leagues count only through their actual rounds.
- **Name matching** (`names.py`): exact / strong / partial (surname + first initial, never a shared given name) / hint (the free-text players line).
- **Tables** (created by `job_queue.init_schema()`): `research_control` (params JSON), `research_runs` (candidates, leads, stats, collector errors), `research_events` (catalog, upserted by (source, source_id), with the operator's per-event `policy`, which re-collection never overwrites).
- **Migration:** the `background` lane needs `python scripts/migrate_research.py` once per database (it rebuilds the `trade_jobs.lane` CHECK; idempotent). Until then, queueing a research job fails.
- **API:** `GET /api/markets/{slug}/research`, `POST …/research/run`, `POST /api/research/events` (add), `DELETE /api/research/events/{id}` (your events only), `PUT /api/research/events/{id}/policy`.
- **Tests:** `tests/test_research.py`. `scripts/research_smoke_test.py` checks minirouter models (billed); `scripts/chesscom_events_probe.py` is the record of the chess.com test.

### Local analysis

Notebooks and backtests run against a local SQLite copy of Railway, in the `tt` conda env (`requirements-dev.txt` plus `kalshi-core`, Jupyter kernel "Python (tt)"). Run every script from `tt`: conda `base` has a psycopg without libpq, so anything touching Postgres fails there with "no pq wrapper available".

- `python scripts/pull_snapshot.py` refreshes `data/chess_tournament_data.db` over a read-only connection. Never put the Railway URL in `.env.local`.
- `python scripts/run_backtest.py --notes "..."` records a run in `data/backtests.db` with its settings and git commit. Pulling a snapshot doesn't affect that file.
- `src/backtest_runs.py` holds the scoring. The run-comparison notebook (`backtest-explorer.ipynb`) is now in `notebooks/archive/`.
- `run_raw` and `run_adjusted` take `as_of` to rebuild a past week's prediction from only earlier history (`load_and_prepare(as_of=)`). Leave it unset in production.
- `notebooks/model-performance-audit.ipynb` audits Titled Tuesday's live predictions since 2026-07-07: calibration, blind spots, manual-override accuracy, Brier scores against backtest models, and betting results.
- `notebooks/bullet-brawl-score-model.ipynb` tests a Bullet Brawl model that splits score into games played × points per game, instead of replaying raw scores.
- `notebooks/archive/` (gitignored, local only) holds retired notebooks.

### Multiple markets

`src/markets/` holds one `MarketSpec` per weekly event: Kalshi series, N values, start and bid-expiry times, and a `ModelConfig`. Trading, snapshots, the pipeline runner and the API all take a spec, defaulting to Titled Tuesday. A spec becomes tradable only when it is registered in `MARKETS` in `src/strategies.py`.

- `pipeline_runs`, `manual_adjustments`, `attendance_conflicts`, both latest-predictions tables, `historical_predictions` and `backtest` have a `market` column. Always filter on it, and write predictions with `db.replace_market_rows`.
- Each market keeps its raw results in its own tables (`titled_tuesday_standings`, `bullet_brawl_standings`).
- `scripts/migrate_multi_market.py` applies this schema to an existing database. It is idempotent.
- The Streamlit app and the trading CLI scripts cover Titled Tuesday only; the React dashboard covers every registered market.
- `ModelConfig.score_noise_sigma` (score points) sets a market's Gaussian smoothing in `run_raw` and `run_adjusted`. Titled Tuesday's is 0; Bullet Brawl's is 10, the winner of its sigma backtest. `ModelConfig.played_filter` is the SQL condition for a row that counts as attending; Bullet Brawl uses `games > 0`.
- `python scripts/run_backtest.py --market bullet_brawl` runs the sigma comparison (its results notebook, `bullet-brawl-backtest.ipynb`, is now in `notebooks/archive/`). Add a market's backtest to `PROFILES` in that script.
- Adding a market: a `MarketSpec` (with `pipeline_steps` and `event_url_example`), a scrape function and `PIPELINES` entry in `scripts/weekly_pipeline.py`, registration in `MARKETS`, and a Railway cron service.
- Bullet Brawl is registered and runs the same weekly workflow as Titled Tuesday. Chess.com has no Bullet Brawl listing page, so `scripts/scraping/update_bullet_brawl.py` finds each new event in the monthly game archives of the most frequent recent players, confirms it against the tournament API, and then enriches new accounts with at least `MIN_APPEARANCES` games into `player_information`. If no archive has the event yet, the step fails with `listing_missing_slug` and the Pipeline page takes the URL.
- Usernames keep chess.com's capitalization in every table, one spelling per account. The tournament API lists Bullet Brawl players in lowercase, so ingestion takes the case from the player's games and then adopts the spelling already on record (`bullet_brawl_ingest.known_usernames`: Titled Tuesday first, then `player_information`). Never lowercase usernames; compare case-insensitively when checking whether an account exists. `scripts/fix_bullet_brawl_username_case.py` repaired the first backfill, which had lowercased them.
- `scripts/scraping/backfill_bullet_brawl.py` loads history into local SQLite and writes `data/bullet_brawl_backfill_report.csv`. `scripts/push_bullet_brawl_history.py` copies that history and the enriched player names to Railway (run once). `pull_snapshot.py` keeps the local rows until Railway has its own.

### Key modules

**`src/config.py`** — Single source of truth for MC hyperparameters (`N_SIMS=100_000`, `SKILL_DECAY=0.96`, `PARTICIPATION_DECAY=0.75`), DB path (local), attendance model cutoffs. **No `TOURN_DATE` constant** — resolve dynamically via `src.pipeline.next_tourn_date(spec)`, which is one week after the latest date in the market's standings table.

**`src/db.py`** — Dual-backend connection layer. `get_conn()` returns a wrapped DB-API connection (auto-translates `?` → `%s` on Postgres); `get_engine()` returns a cached SQLAlchemy engine for pandas-style access. Backend chosen at import time from `$DATABASE_URL`: `postgres(ql)://` → psycopg3, else SQLite at `config.DB_PATH`. Helpers: `upsert_many()`, `replace_market_rows()`, `read_market_rows()`, `table_columns()`.

**`src/data.py`** — `load_and_prepare()` reads standings, computes decay-weighted skill history and EWMA participation rates, down-weights events before `SEASON_SHIFT_DATE`, subtracts `attendance_conflicts` weeks from the EWMA denominator.

**`src/simulation.py`** — Monte Carlo kernels + three player-pool builders:
- `build_player_pool()` + `run_simulation()`: rank-percentile (legacy, float32)
- `build_player_pool_score()`: composite `score * 10000 + tiebreak` (production default)
- `build_player_pool_perf()`: `performance_rating` (aroc-based Elo; used by backtest)

The score and perf pools feed `run_simulation_ranked` (also exported as `run_simulation_score`). Runs in NumPy-vectorized chunks (`CHUNK=10_000` sims per batch). Returns `P_top{1,3,5,8,10}_given_play` + `p_participate`.

**`src/pipeline.py`** — Three public functions:
- `run_raw(tourn_date)` — load → build pool → baseline simulation → optionally save to `latest_model_predictions_raw`.
- `run_adjusted(tourn_date, cut_players=, keep_players=, p_participate_overrides=, p_nudges=, global_nudge=)` — merges adjustments into the raw pool, re-simulates, optionally (`save_official=True`) saves to `latest_model_predictions` and archives the same rows in `historical_predictions`, replacing that market's rows for the date. The archive therefore holds the last official prediction before each event, the one that was traded. Adjustment priority: `p_nudges` → `cut_players` → `p_participate_overrides` → `keep_players`.
- `load_adjustments(tourn_date) -> dict` — reads `manual_adjustments` and shapes the rows into the kwargs `run_adjusted` expects. "Last-write-wins" per (player, type).
- `next_tourn_date(spec=TITLED_TUESDAY) -> str` — one week after the latest event in the market's standings table.

**`src/kalshi_markets.py`** — Kalshi helpers for any market spec; `src/kalshi_tt.py` wraps them as `get_tt_markets()`, `get_tt_asks()`, `get_tt_positions_df()`. The positions reader joins Kalshi's live position data against the local kalshi-core `fills` table so transaction fees are reflected in `cost_incl_fees` and `avg_price_incl_fees`. Import `KalshiClient` from `kalshi_core`, not from here.

**`src/trading.py`** — Four live strategies. All default `tourn_date=None` and lazily resolve via `next_tourn_date()`:
- `place_bids()` — **legacy, superseded by the market maker's anchors**; live submissions are refused while the MM is live and unpaused on that market. Fair price = `p_participate × P_top{N}_given_play`, bids at `max(fair/markup, fair − max_discount)`, cancels stale orders if new bid is lower, pulls back if market ask is at or below the computed bid. Expiry 9 AM ET on tourn day. With `match_on_best_available=True`, if the best competing bid (excluding our own resting orders) is below the computed bid, it rests 1¢ above that bid instead.
- `place_fade_no_bids()` — resting NO bids at `(1 − P_top{N}_given_play) × 100 − discount_cents`, expiring N minutes after 11:00 ET tourn start. Thesis: fade retail panic-buying of YES on low-attendance stars. **Only touches NO bids with expiration ≥ tourn start** so it doesn't wipe the 9 AM `place_bids` orders.
- `take_trades()` — fetches live asks, filters by ROI threshold and min quantity (iceberg-decoy guard), fires fill-or-kill orders via `ThreadPoolExecutor`.
- `cancel_tt_bids()` — cancels every resting TT order for the date.

Bid math (`bid_cents`, `best_ask`, `apply_pullback`, `kalshi_order_price`) imported from `kalshi_core`.

**`src/portfolio.py`** — Portfolio-level MC for P&L simulation and Kelly sizing. `pnl_summary`, `portfolio_kelly`, `kalshi_order_price` re-exported from `kalshi_core` for back-compat.

### Dashboard package (`app/`)

```
app/
    dashboard.py        ← Streamlit entry (auth → banner → 7 tabs)
    auth.py             ← DASHBOARD_PASSWORD gate via session_state
    banner.py           ← Persistent status strip reading pipeline_runs
    trade_loop.py       ← Background take-trades loop thread for the Trade tab
    state.py            ← Cached DB loaders (st.cache_data) — current_status,
                           load_predictions, username_to_player_map, ...
                           Also defines the PipelineStatus NamedTuple.
    tabs/
        odds.py         ← Three Plotly views (chances / fair prices / table) + N selector
        adjustments.py  ← Form writes manual_adjustments; "Save & recompute" fires trigger_adjusted
        pipeline.py     ← Timeline of current + recent pipeline_runs rows; retry controls
        portfolio.py    ← get_tt_positions_df + fee-inclusive metrics; Sync fills button
        trade.py        ← Place / Fade / Take / Cancel sub-sections, each two-tap confirm
        edges.py        ← Current edge trades (compute_edges)
        jobs.py         ← Paginated job_runs log viewer
```

### Database schema

Postgres on Railway (schema defined in `scripts/init_postgres_schema.py`); SQLite locally (same shape, type-tolerant). Key tables:

**TT data:**
- `titled_tuesday_standings` — per-player results per tournament. `date` is `TIMESTAMPTZ` on Postgres; the scraper writes `pd.Timestamp(f'{date} 00:00:00+00:00')` to send a real timestamp (not a VARCHAR, which Postgres refuses to implicit-cast).
- `titled_tuesday_tournaments` — tournament metadata (date, num_players, winner, url).
- `player_information` — Chess.com enrichment. `is_default=1` marks the canonical username when a player has multiple accounts.
- `attendance_conflicts` — (market, date, player_name, username) rows for event dates where a player had an unavoidable broadcast-round conflict. Rebuilt per market at the end of `update_broadcasts.py` (weekly) or standalone via `scripts/build_attendance_conflicts.py --market <slug>`. Consumed by `load_and_prepare()` so `p_participate` reflects attendance rate on conflict-free weeks.
- `other_events` / `other_event_rounds` / `other_event_participants` — concurrent OTB tournaments parsed from Lichess monthly PGN dumps.

**Predictions:**
- `latest_model_predictions_raw` — pre-adjustment predictions (overwritten each `run_raw`).
- `latest_model_predictions` — current adjusted predictions (overwritten each `run_adjusted`).
- `historical_predictions` — archived past predictions; each `run_adjusted(save_official=True)` overwrites its (market, tourn_date) rows.

**Market maker** (created by `job_queue.init_schema()`): `mm_control` (pause flag + param overrides per market), `mm_player_overrides` (wipe factors, pulls, per-player markup), `mm_alerts`, `mm_status` (heartbeat, mode, `paused_ack`, state JSON), `mm_fills`.
- `backtest` — joint walk-forward backtest output. Row = (tourn_date, model, username) with `model ∈ {score, perf}`, includes `played` + `actual_rank`.

**Orchestration (new in the Railway deploy):**
- `pipeline_runs` — one row per weekly cycle. `(tourn_date UNIQUE, state, current_step, failed_step, error_kind, error_message, started_at, completed_at)`. `state ∈ {running, adjustments_pending, completed, failed}`. `error_kind ∈ {listing_missing_slug, http_error, computation_error, unknown}`.
- `job_runs` — fine-grained log of every step. `(pipeline_run_id FK, job_name, started_at, ended_at, status, summary JSONB, log_tail)`. Many `job_runs` rows roll up to one `pipeline_runs`.
- `manual_adjustments` — `(tourn_date, player_name, adjustment_type, value, created_at, created_by)`. `adjustment_type ∈ {cut, keep, override, nudge, global_nudge}`. Consumed by `load_adjustments()`. Dashboard writes `created_by='dashboard'`.

**Kalshi (via kalshi-core shared DB at `~/.kalshi/data/kalshi.db`):**
- `fills` — account-level trade history including `fee_cost`.
- `portfolio_snapshots` — periodic position snapshots.

See `../kalshi-core/CLAUDE.md` for the kalshi-core API.

### Pipeline state machine

```
         [cron fires on Tuesday (TT) or Saturday (BB)]
                 ↓
         ┌─ scrape_tt / scrape_bb ┐
         │                       │
         │   listing_missing_slug → state=failed  (user pastes the URL on the Pipeline page)
         │   http_error           → state=failed  (user hits Retry pipeline)
         ↓                       │
         update_broadcasts       │
         │                       │
         │   http_error           → state=failed
         ↓                       │
         run_raw                 │
         │                       │
         │   computation_error   → state=failed
         ↓                       │
         state = adjustments_pending  (user opens the Attendance page)
                 ↓
         [user edits manual_adjustments]
                 ↓
         [user taps "Save & recompute"]
                 ↓
         run_adjusted
                 ↓
         state = completed  (trading unlocks)
```

Each run is for `spec.most_recent_event()` and is keyed (market, tourn_date). If the cron never started (no row for the latest event once its day has passed), `/status` reports `pipeline_missing` and the dashboard shows a banner and a **Run pipeline for <date>** button. Runs go through the trade queue, so tt-worker must be up.

Spending jobs refuse to fire when the prior event's `pipeline_runs.state != 'completed'` (stale-data guard), unless submitted with the override.

### Username / player-name mapping

Chess.com usernames (in standings) map to display names via `player_information.player_name`. Kalshi contracts are labelled with these display names. `is_default=1` marks the most-recently-active Titled Tuesday username per player.

Players sometimes use a different account per event, so trading and `run_adjusted` use `data.market_player_to_username(usernames, spec)`: among a player's accounts in the market's predictions, the one seen most recently in that market wins. `data.match_player` looks a Kalshi name up exactly, then falls back to an unambiguous word-set match (e.g. "Renato Terry" for "Renato Terry Celis"). Names that still don't match show as unmatched on the Edges page; add them to `_PLAYER_ALIASES` in `src/data.py`.

### Kalshi market tickers

Each `MarketSpec` defines a winner series and a top-N series:
- `KXTITLEDTUESDAY-{date}-{PLAYER}` / `KXBULLETBRAWL-{date}-{PLAYER}` — winner market (N=1)
- `KXTITLEDTUESTOP-{date}T{N}-{PLAYER}` / `KXBULLETBRAWLTOP-{date}T{N}-{PLAYER}` — top-N markets (N=3, 5, 8)

`{date}` is in Kalshi's `YYMONDD` format (e.g. `26SEP16`), produced by `_to_kalshi_date()` from `kalshi_core`. `MarketSpec.n_from_ticker` extracts N from these patterns.

### API cache

Chess.com API responses cached in `data/api_cache/` (JSON keyed by username) with thread-safe access + exponential backoff on rate limits.

## Environment Setup (local dev)

Kalshi credentials live in `~/.kalshi/` (not in this repo):
```
~/.kalshi/
    .env              ← KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV
    trading-key.txt   ← RSA private key for API signing
```

`kalshi_core.KalshiClient()` loads from this location. On Railway, `KalshiClient.from_env()` reads `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_B64` (base64 of the PEM key) from env vars instead.

Database:
- **Local:** nothing to configure — `DB_PATH = data/chess_tournament_data.db` (SQLite, auto-created).
- **Postgres local test:** put a local or disposable Postgres `DATABASE_URL=postgres://...` in `.env.local` (gitignored); `src/db.py` auto-loads it without overriding a variable already set. Never Railway's URL.
