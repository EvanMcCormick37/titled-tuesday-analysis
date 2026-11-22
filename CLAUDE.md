# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@../kalshi-core/CLAUDE.md

## Project Purpose

Production Python trading system for betting on chess.com's weekly **Titled Tuesday** blitz tournament, **Bullet Brawl** 1+0 arena and **Freestyle Friday** chess960 Swiss using Kalshi prediction markets. The system:
1. Scrapes historical standings from chess.com + concurrent-event broadcast data from Lichess
2. Predicts each player's probability of attending and finishing in the top N (Monte Carlo)
3. Places and executes orders on Kalshi using those fair-value estimates

Production runs on **Railway**: several services sharing one Docker image and one Postgres instance. Local development runs against SQLite through the dual-backend DB layer.

## Deployment (Railway)

Services + one Postgres plugin, all sharing the single `Dockerfile` at the repo root:

| Service | Start command | Purpose |
|---|---|---|
| `tt-cron` | `python scripts/weekly_pipeline.py` | Titled Tuesday weekly scrape → broadcasts refresh → raw MC. Cron: `0 19 * * 2` (UTC — 15:00 EDT / 14:00 EST) |
| `bb-cron` | `python scripts/weekly_pipeline.py --market bullet_brawl` | The same pipeline for Bullet Brawl. Cron: `0 12 * * 0` (UTC — Sunday 08:00 EDT / 07:00 EST). It runs the morning after because chess.com's public tournament API caches status: an hour after Saturday's event it can still report the arena unfinished, and the scrape fails |
| `ff-cron` | `python scripts/weekly_pipeline.py --market freestyle_friday` | The same pipeline for Freestyle Friday. Cron: `0 12 * * 6` (UTC — Saturday 08:00 EDT / 07:00 EST), the morning after, for the same API-caching reason as bb-cron |
| `tt-api` | `/bin/sh -c "exec python -m uvicorn api.main:app --host 0.0.0.0 --port $PORT"` | Primary dashboard: FastAPI + built React app (`web/`), every registered market. Healthcheck `/api/me` |
| `tt-app` | `python -m streamlit run app/dashboard.py --server.port=$PORT --server.address=0.0.0.0` | Legacy Streamlit dashboard, Titled Tuesday only |
| `tt-worker` | `python scripts/trade_worker.py` | Runs the trade queue, recomputes, pipeline retries, attendance research, live Kalshi snapshots, and the market-maker watchdog. Run exactly one replica; restart policy ON_FAILURE |
| `tt-market-maker` | `python scripts/market_maker.py` | Reactive two-tier market maker for every registered market (see "Market maker" below). Shadow unless `MM_LIVE=1`. Run exactly one replica; restart policy ON_FAILURE. The code calls it `tt-mm`; tt-worker finds it by its DB heartbeat, so the service name doesn't matter |

**Important Railway gotcha:** Cron expressions are evaluated in UTC regardless of the `TZ` env var. Use numeric day-of-week (`0` = Sunday, `2` = Tuesday, `6` = Saturday), not the `TUE` alias. `TZ=America/New_York` only affects the container's clock inside Python (which matters for `MarketSpec.most_recent_event()`, the date each pipeline run is for).

Env vars (set in Railway UI, per service unless noted):
- **Shared:** `DATABASE_URL` (always the reference `${{Postgres.DATABASE_URL}}`, never a pasted URL: a copied public URL goes stale when the password changes, which crashed tt-worker and tt-cron in 2026-11), `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_B64` (base64 of `~/.kalshi/trading-key.txt`), `KALSHI_ENV=prod`, `TZ=America/New_York`, `GH_TOKEN` (PAT for installing private `kalshi-core`)
- **tt-api and tt-app:** `DASHBOARD_PASSWORD`
- **tt-worker only:** `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30`
- **tt-market-maker only:** `MM_LIVE=1` to send orders (otherwise shadow), optional `MM_SCOUTS=0` (anchors only), `MM_MARKETS` (comma-separated slugs), `MM_JOURNAL_DIR` (default `data/mm_journal`; mount a volume there to keep journals across deploys). Needs a kalshi-core build that includes `KalshiWS`, so push kalshi-core `main` before deploying

**Schema changes are not applied on deploy.** Before deploying code that needs new columns or tables, run the migration against Railway, e.g. `railway ssh` into tt-worker and run `python scripts/migrate_multi_market.py` (it uses the internal `DATABASE_URL`), or from your machine with `DATABASE_URL='<public URL>' python scripts/...` set for that one command. Only the queue tables (`job_queue.init_schema()`) are created automatically, by tt-api and tt-worker at startup.

The `Dockerfile` installs `kalshi-core` at build time from a private GitHub repo using `GH_TOKEN`. Local dev can bind-mount `/opt/kalshi-core` instead — `docker-entrypoint.sh` handles both paths.

## Common Commands

```bash
pip install -r requirements.txt

# Full weekly pipeline (scrape → broadcasts → raw MC → adjustments_pending)
python scripts/weekly_pipeline.py [--market bullet_brawl|freestyle_friday]

# Re-run `run_adjusted` against the current manual_adjustments table
python scripts/trigger_adjusted.py [--market bullet_brawl|freestyle_friday]

# Walk-forward backtest (records the run in data/backtests.db)
python scripts/run_backtest.py [--market bullet_brawl|freestyle_friday] --notes "..."

# Trading CLI — Titled Tuesday only, dry run unless --live; trade the other markets from the React dashboard
python scripts/place_bids.py        # legacy; refused while the MM is live
python scripts/place_fade_bids.py
python scripts/take_trades.py       # optional --loop
python scripts/cancel_bids.py

# Dashboard (local dev)
python -m uvicorn api.main:app --reload     # React dashboard API (all markets)

# Market maker
python scripts/market_maker.py              # shadow: live books, computes quotes, sends nothing
MM_LIVE=1 python scripts/market_maker.py    # live
```

## Architecture Overview

### Entry points (where to start reading)

- **`scripts/weekly_pipeline.py`** — orchestrates a market's three pre-trade steps (`MarketSpec.pipeline_steps`: `scrape_tt`, `scrape_bb` or `scrape_ff` → `update_broadcasts → run_raw → research`) as a resumable state machine. `research` is advisory and never fails the run. Writes to `pipeline_runs` + `job_runs`. On any failure, halts with `state='failed'`, `error_kind` classified for the dashboard to show an appropriate retry UI.
- **`scripts/trigger_adjusted.py`** — `run_adjusted` entry point. Reads `manual_adjustments` for the upcoming tourn_date, re-simulates, writes `latest_model_predictions`, flips the matching `pipeline_runs` row to `completed`.
- **`api/main.py`** + **`web/`** — FastAPI + React dashboard for every registered market (market picker in the top bar). Pages: Trade / Market maker / Edges / Portfolio / Odds / Attendance / Pipeline / Logs. Gated by `DASHBOARD_PASSWORD`.
- **`scripts/market_maker.py`** — tt-market-maker entry point; runs `src/mm/engine.py`.
- **`app/dashboard.py`** — legacy Streamlit dashboard, Titled Tuesday only.

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
- Parameters and their defaults: `MMParams` in `src/mm/config.py` (per-market overrides validated by pydantic, `extra='forbid'`).
- Tables (`mm_control`, `mm_player_overrides`, `mm_alerts`, `mm_status`, `mm_fills`) are created by `job_queue.init_schema()`, so no migration is needed.
- Journal: JSONL of every WS message and action, one file per UTC day in `MM_JOURNAL_DIR`.
- Quoting is active only when the market is unpaused, not held (a model run that fails hard validation holds the market and raises a `model_invalid` alert), fair values are loaded, and the time is before the cutoff (minus 5 s).
- Modules: `config` (`MMParams`, order-id prefixes), `book` (WS books + ex-own view), `quoting` (pure pricing/sizing), `fair_value`, `risk` (position/player/event limits), `oms` (target diffing, token bucket, order states), `gateway` (`LiveGateway` / `ShadowGateway`), `engine` (the asyncio process), `watchdog` (run by tt-worker), `store` (`mm_*` tables), `journal`.
- API: `GET /api/markets/{slug}/mm` (status, quotes, alerts, fills), `POST …/mm/pause`, `PUT …/mm/params`, `POST …/mm/players/{username}/repost|pull`, `POST …/mm/alerts/ack-all`, `POST /api/mm/alerts/{id}/ack`. The price-war governor rests at the buffer edge silently (no alert).
- Tests: `tests/test_mm_core.py` (quoting, book, risk, OMS) and `tests/test_mm_engine.py` (engine with a fake gateway).

### Attendance researcher (`src/research/`)

Finds other tournaments that may keep high-value players away from the next event. It is deterministic so far, with no LLM; the planned LLM phases are in `Attendance Researcher Architecture — Proposal.md` at the repo root.

- Runs automatically as the last step of the weekly pipeline (`research`, right after `run_raw`, for the event `run_raw` predicted). A research failure is recorded in `research_runs` and the step summary but never fails the pipeline. It can also be re-run as the `research` ops job in the **`background` lane**: a subprocess on tt-worker (`scripts/research_attendance.py --market --date`) that never holds the market's trade lane, started from **Run now** on the Attendance page. The kill switch leaves it alone.
- **Candidates:** players whose raw-model fair value `p_participate × P_top8_given_play` is above 5% (`candidate_min_fv`, `candidate_n`), one row per player.
- **Calendar:** the Lichess broadcast API (`/api/broadcast/top` + `/api/broadcast`, every tier, then rounds and `/broadcast/{id}/players` for events within a day of the event), plus events you add on the Attendance page. chess.com/events can't be read automatically (Cloudflare challenges headless browsers); add its events by hand. Don't add stealth or challenge-solving.
- **Clash test** (`overlap.py`): window = start to start + `MarketSpec.event_duration_minutes` (TT 150, BB 120). A round overlapping the window is `hard`, within `adjacent_hours` is `adjacent`, within `mid_event_hours` is `multi_day`. Round length depends on time control (classical 300 min). Season-long leagues count only through their actual rounds.
- **Name matching** (`names.py`): exact / strong / partial (surname + first initial, never a shared given name) / hint (the free-text players line).
- **Tables** (created by `job_queue.init_schema()`): `research_control` (params JSON), `research_runs` (candidates, leads, stats, collector errors), `research_events` (catalog, upserted by (source, source_id), with the operator's per-event `policy`, which re-collection never overwrites).
- **Migration:** the `background` lane needs `python scripts/migrate_research.py` once per database (it rebuilds the `trade_jobs.lane` CHECK; idempotent). Until then, queueing a research job fails.
- **Tests:** `tests/test_research.py`.

### Local analysis

Notebooks and backtests run against a local SQLite copy of Railway, in the `tt` conda env (`requirements-dev.txt` plus `kalshi-core`, Jupyter kernel "Python (tt)"). Run every script from `tt`: conda `base` has a psycopg without libpq, so anything touching Postgres fails there with "no pq wrapper available".

- `python scripts/pull_snapshot.py` refreshes `data/chess_tournament_data.db` over a read-only connection. Never put the Railway URL in `.env.local`.
- `python scripts/run_backtest.py --notes "..."` records a run in `data/backtests.db` with its settings and git commit. Pulling a snapshot doesn't affect that file. `src/backtest_runs.py` holds the scoring.
- Judge models with `python scripts/score_vip.py <run ids>`: the **VIP Brier score**, on the fixed cohort of traded players in `src/vip_players.py`, against each player's own top-N rate over the previous year (`skill` > 0 beats it), with price and attendance calibration and bootstrap ranges over events. `--drop-conflicts` leaves out week-of conflicted pairs, as manual cuts would. Whole-field log loss is dominated by long shots that are never traded; keep the cohort fixed so no model chooses which players count.
- `run_raw` and `run_adjusted` take `as_of` to rebuild a past week's prediction from only earlier history (`load_and_prepare(as_of=)`). Leave it unset in production.
- `notebooks/model-performance-audit.ipynb` audits Titled Tuesday's live predictions since 2026-07-07: calibration, blind spots, manual-override accuracy, Brier scores against backtest models, and betting results.
- `notebooks/bullet-brawl-score-model.ipynb` tests a Bullet Brawl model that splits score into games played × points per game, instead of replaying raw scores.

### Multiple markets

`src/markets/` holds one `MarketSpec` per weekly event: Kalshi series, N values, start and bid-expiry times, and a `ModelConfig`. Trading, snapshots, the pipeline runner and the API all take a spec, defaulting to Titled Tuesday. A spec becomes tradable only when it is registered in `MARKETS` in `src/strategies.py`.

- `pipeline_runs`, `manual_adjustments`, `attendance_conflicts`, both latest-predictions tables, `historical_predictions` and `backtest` have a `market` column. Always filter on it, and write predictions with `db.replace_market_rows`.
- Each market keeps its raw results in its own tables (`titled_tuesday_standings`, `bullet_brawl_standings`, `freestyle_friday_standings`). Freestyle Friday's has Titled Tuesday's columns, so the same score model applies.
- `scripts/migrate_multi_market.py` applies this schema to an existing database. It is idempotent.
- The Streamlit app and the trading CLI scripts cover Titled Tuesday only; the React dashboard covers every registered market.
- `ModelConfig.score_noise_sigma` (score points) sets a market's Gaussian smoothing in `run_raw` and `run_adjusted`. Titled Tuesday's is 0.01 (100 composite units, `SCORE_GAUSSIAN_KERNEL_SIGMA_NARROW`), which randomizes tiebreaks without moving anyone across a score bucket; Bullet Brawl's is 10, the winner of its sigma backtest; Freestyle Friday uses Titled Tuesday's 0.01. Never set it to 0: replayed results would keep their exact historical tiebreaks, so ties resolve deterministically. The `ModelConfig` default is 0.01 for that reason. `ModelConfig.played_filter` is the SQL condition for a row that counts as attending; Bullet Brawl uses `games > 0`.
- Optional model settings, all off by default: `ModelConfig.attendance_model='two_part'` (`src/attendance_model.py`: a walk-forward logistic model of the choice to play, fitted on conflict-free weeks from a fast and a slow attendance rate, weeks since last played, appearances and past conflict rate); `conflict_deduction=False` keeps past conflict weeks in the attendance denominator; `skill_prior_events=k` mixes the field's recent results into each player's skill history with weight k / (k + effective events). `run_backtest.py` compares them as `narrow_*` variants (`BacktestProfile.overrides`).
- `python scripts/run_backtest.py --market bullet_brawl` runs the sigma comparison. Add a market's backtest to `PROFILES` in that script.
- Adding a market: a `MarketSpec` (with `pipeline_steps` and `event_url_example`), a scrape function and `PIPELINES` entry in `scripts/weekly_pipeline.py`, registration in `MARKETS`, and a Railway cron service.
- Chess.com has no Bullet Brawl listing page, so `scripts/scraping/update_bullet_brawl.py` finds each new event in the monthly game archives of the most frequent recent players, confirms it against the tournament API, and then enriches new accounts with at least `MIN_APPEARANCES` games into `player_information`. If no archive has the event yet, the step fails with `listing_missing_slug` and the Pipeline page takes the URL.
- Freestyle Friday (11-round Swiss, chess960 3+2, Fridays 11:00 ET) is a normal chess.com live tournament, `freestyle-friday-{month}-{dd}-{yyyy}-{id}`. `scripts/scraping/update_freestyle_friday.py` discovers events the same way (regulars' archives, confirmed as a finished chess960 Swiss on a Friday) and parses the standings pages with Titled Tuesday's `fetch_standings`. History runs weekly from 2025-01-24 to 2026-06-26, then from 2026-10-02 after a July–September 2026 hiatus (2026-05-08 was cancelled). `--backfill --start <month> --until <month>` walks back through regulars' archives, across empty months; without `--until` it stops after two empty months, so it would stop at the hiatus. Five events (US/EU daylight-saving mismatch weeks) started at 12:00 ET, an hour after the spec's start time. `scripts/push_bullet_brawl_history.py --market freestyle_friday` copies the local history to Railway. The chess.com/events page for it renders client-side and isn't used.
- Usernames keep chess.com's capitalization in every table, one spelling per account. The tournament API lists Bullet Brawl players in lowercase, so ingestion takes the case from the player's games and then adopts the spelling already on record (`bullet_brawl_ingest.known_usernames`: Titled Tuesday first, then `player_information`). Never lowercase usernames; compare case-insensitively when checking whether an account exists.

### Key modules

- **`src/config.py`** — MC hyperparameters (`N_SIMS`, `SKILL_DECAY`, `PARTICIPATION_DECAY`), local DB path, attendance cutoffs. **No `TOURN_DATE` constant** — use `src.pipeline.next_tourn_date(spec)` (one week after the latest date in the market's standings table).
- **`src/db.py`** — dual backend chosen at import from `$DATABASE_URL` (`postgres(ql)://` → psycopg3, else SQLite at `config.DB_PATH`). `get_conn()` translates `?` → `%s` on Postgres; `get_engine()` for pandas.
- **`src/data.py`** — `load_and_prepare()`: decay-weighted skill history and EWMA participation, with `attendance_conflicts` weeks removed from the denominator.
- **`src/simulation.py`** — MC kernels. Production uses `build_player_pool_score()` (composite `score * 10000 + tiebreak`) with `run_simulation_ranked`; the perf pool is for backtests.
- **`src/pipeline.py`** — `run_raw`, `run_adjusted`, `load_adjustments`, `next_tourn_date`. `run_adjusted(save_official=True)` also archives to `historical_predictions`. Adjustment priority: `p_nudges` → `cut_players` → `p_participate_overrides` → `keep_players`; last write wins per (player, type).
- **`src/kalshi_markets.py`** / **`src/kalshi_tt.py`** — Kalshi helpers per spec; positions join the kalshi-core `fills` table for fee-inclusive cost. Import `KalshiClient` from `kalshi_core`, not from here.
- **`src/trading.py`** — `place_bids` (legacy), `place_fade_no_bids`, `take_trades`, `cancel_tt_bids`. Fade bids only touch NO orders expiring at or after the event start, so they never cancel the earlier-expiring bids.
- **`src/portfolio.py`** — portfolio MC and Kelly sizing. tt-worker runs `scripts/portfolio_pnl.py` as a subprocess for the Portfolio page's `pnl` snapshot, only when positions or predictions change.

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
- `historical_predictions` — archived past predictions; each `run_adjusted(save_official=True)` overwrites its (market, tourn_date) rows, so it holds the last official prediction before each event, the one that was traded.
- `backtest` — joint walk-forward backtest output. Row = (market, tourn_date, model, username) with `model ∈ {score, perf}`, includes `played` + `actual_rank`.

**Market maker** (created by `job_queue.init_schema()`): `mm_control` (pause flag + param overrides per market), `mm_player_overrides` (wipe factors, pulls, per-player markup), `mm_alerts`, `mm_status` (heartbeat, mode, `paused_ack`, state JSON), `mm_fills`.

**Orchestration:**
- `pipeline_runs` — one row per weekly cycle per market. `(market, tourn_date) UNIQUE` (rekeyed by `migrate_multi_market.py`), plus `state, current_step, failed_step, error_kind, error_message, started_at, completed_at`. `state ∈ {running, adjustments_pending, completed, failed}`. `error_kind ∈ {listing_missing_slug, http_error, computation_error, unknown}`.
- `job_runs` — fine-grained log of every step. `(pipeline_run_id FK, job_name, started_at, ended_at, status, summary JSONB, log_tail)`. Many `job_runs` rows roll up to one `pipeline_runs`.
- `manual_adjustments` — `(market, tourn_date, player_name, adjustment_type, value, created_at, created_by)`. `adjustment_type ∈ {cut, keep, override, nudge, global_nudge}`. Consumed by `load_adjustments()`. Dashboard writes `created_by='dashboard'`.

**Kalshi (via kalshi-core shared DB at `~/.kalshi/data/kalshi.db`):**
- `fills` — account-level trade history including `fee_cost`.
- `portfolio_snapshots` — periodic position snapshots.

### Pipeline state machine

```
         [cron fires on Tuesday (TT) or Saturday (BB)]
                 ↓
         ┌─ scrape_tt/_bb/_ff ────┐
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
         research  (advisory; never fails the run)
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
- `KXTITLEDTUESDAY-{date}-{PLAYER}` / `KXBULLETBRAWL-{date}-{PLAYER}` / `KXFSFRIDAY-{date}-{PLAYER}` — winner market (N=1)
- `KXTITLEDTUESTOP-{date}T{N}-{PLAYER}` / `KXBULLETBRAWLTOP-{date}T{N}-{PLAYER}` / `KXFSFRIDAYTOP-{date}T{N}-{PLAYER}` — top-N markets (N=3, 5, 8)

`{date}` is in Kalshi's `YYMONDD` format (e.g. `26SEP16`), produced by `_to_kalshi_date()` from `kalshi_core`. `MarketSpec.n_from_ticker` extracts N from these patterns.

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
