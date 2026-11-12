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
| `tt-worker` | `python scripts/trade_worker.py` | Runs the trade queue, recomputes, pipeline retries, and live Kalshi snapshots. Run exactly one replica; restart policy ON_FAILURE |

**Important Railway gotcha:** Cron expressions are evaluated in UTC regardless of the `TZ` env var. Use numeric day-of-week (`2` = Tuesday, `6` = Saturday), not the `TUE` alias. `TZ=America/New_York` only affects the container's clock inside Python (which matters for `_most_recent_tuesday()`).

Env vars (set in Railway UI, per service unless noted):
- **Shared:** `DATABASE_URL` (from Postgres plugin), `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_B64` (base64 of `~/.kalshi/trading-key.txt`), `KALSHI_ENV=prod`, `TZ=America/New_York`, `GH_TOKEN` (PAT for installing private `kalshi-core`)
- **tt-app only:** `DASHBOARD_PASSWORD`

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
- **`api/main.py`** + **`web/`** — FastAPI + React dashboard for every registered market (market picker in the top bar). Pages: Trade / Edges / Portfolio / Odds / Attendance / Pipeline / Logs. Gated by `DASHBOARD_PASSWORD`.
- **`app/dashboard.py`** — legacy Streamlit entry, Titled Tuesday only. Six tabs: Odds / Attendance / Pipeline / Portfolio / Trade / Jobs.

### Trade queue

All dashboard trading goes through one Postgres-backed queue. `tt-api` only adds jobs; `tt-worker` is the only process that sends them.

- **`src/trade_plan.py`** — `Plan`, and `execute_plan()`, the single code path that sends orders. Progress goes through an `emit` callback, not `print`.
- **`src/trading.py`** — read-only `plan_*` functions; the older `place_bids` etc. are print-based wrappers for the CLI and notebook.
- **`src/strategies.py`** — registry of markets, strategies, param schemas, ops jobs, and snapshots. Add a market here. Forms in the UI are generated from the param schemas.
- **`src/job_queue.py`** — `trade_jobs`, `trade_job_events`, `market_snapshots`. Created automatically by `init_schema()` on API and worker start.
- **`src/job_runner.py`** — `JobRunner` and `Worker`.

Rules: jobs **re-plan when they fire**, and a plan above the approved `max_exposure` halts as `needs_review`. Spending strategies also halt if the prior week's `pipeline_runs` row isn't `completed`. One normal-lane job runs per market (enforced by a partial unique index), and recomputes share that lane because they briefly empty the predictions table. The kill switch (`enqueue_kill`) uses the priority lane. Live jobs are never auto-retried: a job whose worker dies becomes `interrupted`. Unless given `not_after`, a one-shot expires 15 minutes after its intended start and a loop ends at event start. For a loop, `max_exposure` is the total budget.

Tests: `python -m pytest tests` (fake Kalshi client; set `TEST_PG_URL` to a disposable Postgres to also run against Postgres — tables are dropped). Frontend: `cd web && npm run dev` with the API on port 8000. `DASHBOARD_PASSWORD` is required (login fails closed); set `TT_INSECURE_COOKIE=1` for plain-http local testing.

### Local analysis

Notebooks and backtests run against a local SQLite copy of Railway, in the `tt` conda env (`requirements-dev.txt` plus `kalshi-core`, Jupyter kernel "Python (tt)").

- `python scripts/pull_snapshot.py` refreshes `data/chess_tournament_data.db` over a read-only connection. Never put the Railway URL in `.env.local`.
- `python scripts/run_backtest.py --notes "..."` records a run in `data/backtests.db` with its settings and git commit. Pulling a snapshot doesn't affect that file.
- `notebooks/backtest-explorer.ipynb` lists and compares runs; `src/backtest_runs.py` holds the scoring.

### Multiple markets

`src/markets/` holds one `MarketSpec` per weekly event: Kalshi series, N values, start and bid-expiry times, and a `ModelConfig`. Trading, snapshots, the pipeline runner and the API all take a spec, defaulting to Titled Tuesday. A spec becomes tradable only when it is registered in `MARKETS` in `src/strategies.py`.

- `pipeline_runs`, `manual_adjustments`, `attendance_conflicts`, both latest-predictions tables, `historical_predictions` and `backtest` have a `market` column. Always filter on it, and write predictions with `db.replace_market_rows`.
- Each market keeps its raw results in its own tables (`titled_tuesday_standings`, `bullet_brawl_standings`).
- `scripts/migrate_multi_market.py` applies this schema to an existing database. It is idempotent.
- The Streamlit app and the trading CLI scripts cover Titled Tuesday only; the React dashboard covers every registered market.
- `ModelConfig.score_noise_sigma` (score points) sets a market's Gaussian smoothing in `run_raw` and `run_adjusted`. Titled Tuesday's is 0; Bullet Brawl's is 10, the winner of its sigma backtest. `ModelConfig.played_filter` is the SQL condition for a row that counts as attending; Bullet Brawl uses `games > 0`.
- `python scripts/run_backtest.py --market bullet_brawl` runs the sigma comparison, and `notebooks/bullet-brawl-backtest.ipynb` runs or reuses it and shows the results. Add a market's backtest to `PROFILES` in that script.
- Adding a market: a `MarketSpec` (with `pipeline_steps` and `event_url_example`), a scrape function and `PIPELINES` entry in `scripts/weekly_pipeline.py`, registration in `MARKETS`, and a Railway cron service.
- Bullet Brawl is registered and runs the same weekly workflow as Titled Tuesday. Chess.com has no Bullet Brawl listing page, so `scripts/scraping/update_bullet_brawl.py` finds each new event in the monthly game archives of the most frequent recent players, confirms it against the tournament API, and then enriches new accounts with at least `MIN_APPEARANCES` games into `player_information`. If no archive has the event yet, the step fails with `listing_missing_slug` and the Pipeline page takes the URL.
- Usernames keep chess.com's capitalization in every table, one spelling per account. The tournament API lists Bullet Brawl players in lowercase, so ingestion takes the case from the player's games and then adopts the spelling already on record (`bullet_brawl_ingest.known_usernames`: Titled Tuesday first, then `player_information`). Never lowercase usernames; compare case-insensitively when checking whether an account exists. `scripts/fix_bullet_brawl_username_case.py` repaired the first backfill, which had lowercased them.
- `scripts/scraping/backfill_bullet_brawl.py` loads history into local SQLite and writes `data/bullet_brawl_backfill_report.csv`. `scripts/push_bullet_brawl_history.py` copies that history and the enriched player names to Railway (run once). `pull_snapshot.py` keeps the local rows until Railway has its own.

### Key modules

**`src/config.py`** — Single source of truth for MC hyperparameters (`N_SIMS=100_000`, `SKILL_DECAY=0.96`, `PARTICIPATION_DECAY=0.75`), DB path (local), attendance model cutoffs. **No `TOURN_DATE` constant** — resolve dynamically via `src.pipeline.next_tourn_date()` which queries `MAX(titled_tuesday_standings.date) + 1 week`.

**`src/db.py`** — Dual-backend connection layer. `get_conn()` returns a wrapped DB-API connection (auto-translates `?` → `%s` on Postgres); `get_engine()` returns a cached SQLAlchemy engine for pandas-style access. Backend chosen at import time from `$DATABASE_URL`: `postgres(ql)://` → psycopg3, else SQLite at `config.DB_PATH`. Helpers: `upsert_many()`, `replace_market_rows()`, `read_market_rows()`, `table_columns()`.

**`src/data.py`** — `load_and_prepare()` reads standings, computes decay-weighted skill history and EWMA participation rates, down-weights events before `SEASON_SHIFT_DATE`, subtracts `attendance_conflicts` weeks from the EWMA denominator.

**`src/simulation.py`** — Monte Carlo kernels + three player-pool builders:
- `build_player_pool()` + `run_simulation()`: rank-percentile (legacy, float32)
- `build_player_pool_score()`: composite `score * 10000 + tiebreak` (production default)
- `build_player_pool_perf()`: `performance_rating` (aroc-based Elo; used by backtest)

The score and perf pools feed `run_simulation_ranked` (also exported as `run_simulation_score`). Runs in NumPy-vectorized chunks (`CHUNK=10_000` sims per batch). Returns `P_top{1,3,5,8,10}_given_play` + `p_participate`.

**`src/pipeline.py`** — Three public functions:
- `run_raw(tourn_date)` — load → build pool → baseline simulation → optionally save to `latest_model_predictions_raw`.
- `run_adjusted(tourn_date, cut_players=, keep_players=, p_participate_overrides=, p_nudges=, global_nudge=)` — merges adjustments into the raw pool, re-simulates, optionally saves to `latest_model_predictions`. Adjustment priority: `p_nudges` → `cut_players` → `p_participate_overrides` → `keep_players`.
- `load_adjustments(tourn_date) -> dict` — reads `manual_adjustments` and shapes the rows into the kwargs `run_adjusted` expects. "Last-write-wins" per (player, type).
- `next_tourn_date() -> str` — the one Tuesday after `MAX(titled_tuesday_standings.date)`.

**`src/kalshi_markets.py`** — Kalshi helpers for any market spec; `src/kalshi_tt.py` wraps them as `get_tt_markets()`, `get_tt_asks()`, `get_tt_positions_df()`. The positions reader joins Kalshi's live position data against the local kalshi-core `fills` table so transaction fees are reflected in `cost_incl_fees` and `avg_price_incl_fees`. Import `KalshiClient` from `kalshi_core`, not from here.

**`src/trading.py`** — Four live strategies. All default `tourn_date=None` and lazily resolve via `next_tourn_date()`:
- `place_bids()` — fair price = `p_participate × P_top{N}_given_play`, bids at `max(fair/markup, fair − max_discount)`, cancels stale orders if new bid is lower, pulls back if market ask is at or below the computed bid. Expiry 9 AM ET on tourn day.
- `place_fade_no_bids()` — resting NO bids at `(1 − P_top{N}_given_play) × 100 − discount_cents`, expiring N minutes after 11:00 ET tourn start. Thesis: fade retail panic-buying of YES on low-attendance stars. **Only touches NO bids with expiration ≥ tourn start** so it doesn't wipe the 9 AM `place_bids` orders.
- `take_trades()` — fetches live asks, filters by ROI threshold and min quantity (iceberg-decoy guard), fires fill-or-kill orders via `ThreadPoolExecutor`.
- `cancel_tt_bids()` — cancels every resting TT order for the date.

Bid math (`bid_cents`, `best_ask`, `apply_pullback`, `kalshi_order_price`) imported from `kalshi_core`.

**`src/portfolio.py`** — Portfolio-level MC for P&L simulation and Kelly sizing. `pnl_summary`, `portfolio_kelly`, `kalshi_order_price` re-exported from `kalshi_core` for back-compat.

### Dashboard package (`app/`)

```
app/
    dashboard.py        ← Streamlit entry (auth → banner → 6 tabs)
    auth.py             ← DASHBOARD_PASSWORD gate via session_state
    banner.py           ← Persistent status strip reading pipeline_runs
    state.py            ← Cached DB loaders (st.cache_data) — current_status,
                           load_predictions, username_to_player_map, ...
                           Also defines the PipelineStatus NamedTuple.
    tabs/
        odds.py         ← Three Plotly views (chances / fair prices / table) + N selector
        adjustments.py  ← Form writes manual_adjustments; "Save & recompute" fires trigger_adjusted
        pipeline.py     ← Timeline of current + recent pipeline_runs rows; retry controls
        portfolio.py    ← get_tt_positions_df + fee-inclusive metrics; Sync fills button
        trade.py        ← Place / Fade / Take / Cancel sub-sections, each two-tap confirm
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
- `historical_predictions` — archived past predictions.
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
- **Railway-like local test:** put `DATABASE_URL=postgres://...` in `.env.local` (gitignored); `src/db.py` auto-loads it.
