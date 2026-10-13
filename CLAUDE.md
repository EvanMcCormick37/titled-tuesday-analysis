# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@../kalshi-core/CLAUDE.md

## Project Purpose

Production Python trading system for betting on chess.com's weekly **Titled Tuesday** blitz tournament using Kalshi prediction markets. The system:
1. Scrapes historical TT standings from chess.com + concurrent-event broadcast data from Lichess
2. Predicts each player's probability of attending and finishing in the top N (Monte Carlo)
3. Places and executes orders on Kalshi using those fair-value estimates

As of 2026-10, the system is deployed on **Railway** across three services sharing one Docker image + one Postgres instance. Local development against SQLite still works unchanged via the dual-backend DB layer.

## Deployment (Railway)

Three services + one Postgres plugin, all sharing the single `Dockerfile` at the repo root:

| Service | Start command | Purpose |
|---|---|---|
| `tt-cron` | `python scripts/weekly_pipeline.py` | Weekly scrape → broadcasts refresh → raw MC. Cron: `0 19 * * 2` (UTC — 15:00 EDT / 14:00 EST) |
| `tt-app` | `python -m streamlit run app/dashboard.py --server.port=$PORT --server.address=0.0.0.0` | Mobile-friendly Streamlit dashboard — primary user interface |
| `tt-bot` | `python app/telegram_bot.py` | Telegram push alerts + command surface, bound to one chat |

**Important Railway gotcha:** Cron expressions are evaluated in UTC regardless of the `TZ` env var. Use numeric day-of-week (`2` = Tuesday), not the `TUE` alias. `TZ=America/New_York` only affects the container's clock inside Python (which matters for `_most_recent_tuesday()`).

Env vars (set in Railway UI, per service unless noted):
- **Shared:** `DATABASE_URL` (from Postgres plugin), `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_B64` (base64 of `~/.kalshi/trading-key.txt`), `KALSHI_ENV=prod`, `TZ=America/New_York`, `GH_TOKEN` (PAT for installing private `kalshi-core`)
- **tt-app only:** `DASHBOARD_PASSWORD`
- **tt-bot only:** `TELEGRAM_BOT_TOKEN` (from BotFather), `TELEGRAM_CHAT_ID`

The `Dockerfile` installs `kalshi-core` at build time from a private GitHub repo using `GH_TOKEN`. Local dev can bind-mount `/opt/kalshi-core` instead — `docker-entrypoint.sh` handles both paths.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# ── Local one-off tasks ─────────────────────────────────────────────────────

# Full weekly pipeline (scrape → broadcasts → raw MC → adjustments_pending)
python scripts/weekly_pipeline.py

# Just scrape new TT standings
python scripts/scraping/update_titled_tuesday.py

# Weekly broadcast refresh (Lichess PGN dumps + attendance_conflicts rebuild)
python scripts/scraping/update_broadcasts.py

# Re-run `run_adjusted` against the current manual_adjustments table
python scripts/trigger_adjusted.py

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

# ── Dashboard / bot (local dev) ─────────────────────────────────────────────

python -m streamlit run app/dashboard.py
python app/telegram_bot.py
```

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
           │              ┌─────────────────────────┼──────────────────────────┐
           │              ↓                         ↓                          ↓
           │       app/dashboard.py          app/telegram_bot.py        src/trading.py
           │       (Streamlit UI)            (push alerts + cmds)       (place / take / cancel / fade)
           │              │                         │                          ↓
           └─ pipeline_runs                                              kalshi_core.KalshiClient
              (state machine for                                               ↓
              the whole weekly cycle)                                      Kalshi API
```

### Entry points (where to start reading)

- **`scripts/weekly_pipeline.py`** — orchestrates the three pre-trade steps (`scrape_tt → update_broadcasts → run_raw`) as a resumable state machine. Writes to `pipeline_runs` + `job_runs`. On any failure, halts with `state='failed'`, `error_kind` classified for the dashboard/bot to show an appropriate retry UI.
- **`scripts/trigger_adjusted.py`** — `run_adjusted` entry point. Reads `manual_adjustments` for the upcoming tourn_date, re-simulates, writes `latest_model_predictions`, flips the matching `pipeline_runs` row to `completed`.
- **`app/dashboard.py`** — Streamlit entry. Six tabs: Odds / Attendance / Pipeline / Portfolio / Trade / Jobs. Gated by `DASHBOARD_PASSWORD`.
- **`app/telegram_bot.py`** — python-telegram-bot v21 Application. Push alerts via `StateWatcher` (30s poll of `pipeline_runs`). Read-only commands (/status /odds /top /conflicts /positions /orders) + write commands with inline Confirm/Cancel buttons (/place /take /fade /cancel /recompute /retry /cut /keep /override). All handlers gated to `TELEGRAM_CHAT_ID`.

### Key modules

**`src/config.py`** — Single source of truth for MC hyperparameters (`N_SIMS=100_000`, `SKILL_DECAY=0.96`, `PARTICIPATION_DECAY=0.75`), DB path (local), attendance model cutoffs. **No `TOURN_DATE` constant** — resolve dynamically via `src.pipeline.next_tourn_date()` which queries `MAX(titled_tuesday_standings.date) + 1 week`.

**`src/db.py`** — Dual-backend connection layer. `get_conn()` returns a wrapped DB-API connection (auto-translates `?` → `%s` on Postgres); `get_engine()` returns a cached SQLAlchemy engine for pandas-style access. Backend chosen at import time from `$DATABASE_URL`: `postgres(ql)://` → psycopg3, else SQLite at `config.DB_PATH`. Helpers: `upsert_many()`, `write_df_replace()`, `table_columns()`.

**`src/data.py`** — `load_and_prepare()` reads standings, computes decay-weighted skill history and EWMA participation rates, applies an isotonic regression floor (`models/isotonic_regression.joblib`) to smooth low-attendance players, subtracts `attendance_conflicts` weeks from the EWMA denominator.

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

**`src/kalshi_tt.py`** — TT-specific Kalshi helpers: `get_tt_markets()`, `get_tt_asks()`, `get_tt_positions_df()`. The positions reader joins Kalshi's live position data against the local kalshi-core `fills` table so transaction fees are reflected in `cost_incl_fees` and `avg_price_incl_fees`. Import `KalshiClient` from `kalshi_core`, not from here.

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
                           Re-exports PipelineStatus NamedTuple from app/bot/db_reads.py.
    telegram_bot.py     ← tt-bot entry (shares the project; just a different start cmd)
    tabs/
        odds.py         ← Three Plotly views (chances / fair prices / table) + N selector
        adjustments.py  ← Form writes manual_adjustments; "Save & recompute" fires trigger_adjusted
        pipeline.py     ← Timeline of current + recent pipeline_runs rows; retry controls
        portfolio.py    ← get_tt_positions_df + fee-inclusive metrics; Sync fills button
        trade.py        ← Place / Fade / Take / Cancel sub-sections, each two-tap confirm
        jobs.py         ← Paginated job_runs log viewer
    bot/
        __init__.py
        db_reads.py     ← Non-cached mirrors of app/state.py (bot has no st context).
                           Also home of the shared PipelineStatus NamedTuple.
        formatters.py   ← Pure text builders for Telegram (HTML parse_mode)
        alerts.py       ← StateWatcher — polls pipeline_runs every 30s, diffs vs
                           in-memory last-seen, pushes transition alerts.
        handlers.py     ← Command + callback handlers. Write commands stash a
                           PendingPlan behind a 10-char token with 10-min TTL;
                           inline Confirm/Cancel buttons re-run with dry_run=False.
```

### Database schema

Postgres on Railway (schema defined in `scripts/init_postgres_schema.py`); SQLite locally (same shape, type-tolerant). Key tables:

**TT data:**
- `titled_tuesday_standings` — per-player results per tournament. `date` is `TIMESTAMPTZ` on Postgres; the scraper writes `pd.Timestamp(f'{date} 00:00:00+00:00')` to send a real timestamp (not a VARCHAR, which Postgres refuses to implicit-cast).
- `titled_tuesday_tournaments` — tournament metadata (date, num_players, winner, url).
- `player_information` — Chess.com enrichment. `is_default=1` marks the canonical username when a player has multiple accounts.
- `attendance_conflicts` — (date, player_name, username) rows for TT dates where a player had an unavoidable broadcast-round conflict. Rebuilt at the end of `update_broadcasts.py` (weekly) or standalone via `scripts/build_attendance_conflicts.py`. Consumed by `load_and_prepare()` so `p_participate` reflects attendance rate on conflict-free weeks.
- `other_events` / `other_event_rounds` / `other_event_participants` — concurrent OTB tournaments parsed from Lichess monthly PGN dumps.

**Predictions:**
- `latest_model_predictions_raw` — pre-adjustment predictions (overwritten each `run_raw`).
- `latest_model_predictions` — current adjusted predictions (overwritten each `run_adjusted`).
- `historical_predictions` — archived past predictions.
- `backtest` — joint walk-forward backtest output. Row = (tourn_date, model, username) with `model ∈ {score, perf}`, includes `played` + `actual_rank`.

**Orchestration (new in the Railway deploy):**
- `pipeline_runs` — one row per weekly cycle. `(tourn_date UNIQUE, state, current_step, failed_step, error_kind, error_message, started_at, completed_at)`. `state ∈ {running, adjustments_pending, completed, failed}`. `error_kind ∈ {listing_missing_slug, http_error, computation_error, unknown}`.
- `job_runs` — fine-grained log of every step. `(pipeline_run_id FK, job_name, started_at, ended_at, status, summary JSONB, log_tail)`. Many `job_runs` rows roll up to one `pipeline_runs`.
- `manual_adjustments` — `(tourn_date, player_name, adjustment_type, value, created_at, created_by)`. `adjustment_type ∈ {cut, keep, override, nudge, global_nudge}`. Consumed by `load_adjustments()`. Dashboard writes `created_by='dashboard'`; bot writes `created_by='telegram'`.

**Kalshi (via kalshi-core shared DB at `~/.kalshi/data/kalshi.db`):**
- `fills` — account-level trade history including `fee_cost`.
- `portfolio_snapshots` — periodic position snapshots.

See `../kalshi-core/CLAUDE.md` for the kalshi-core API.

### Pipeline state machine

```
         [cron fires on Tuesday]
                 ↓
         ┌─ scrape_tt ───────────┐
         │                       │
         │   listing_missing_slug → state=failed  (user supplies URL via /retry <url>)
         │   http_error           → state=failed  (user hits /retry)
         ↓                       │
         update_broadcasts       │
         │                       │
         │   http_error           → state=failed
         ↓                       │
         run_raw                 │
         │                       │
         │   computation_error   → state=failed
         ↓                       │
         state = adjustments_pending  (bot pushes alert; user opens dashboard)
                 ↓
         [user edits manual_adjustments]
                 ↓
         [user taps "Save & recompute" or sends /recompute]
                 ↓
         run_adjusted
                 ↓
         state = completed  (bot pushes alert; trade commands unlock)
```

The dashboard **Trade** tab and bot write commands refuse to fire when `pipeline_runs.state != 'completed'` (stale-data guard), with an explicit override checkbox for the dashboard case.

### Username / player-name mapping

Chess.com usernames (in standings) map to display names via `player_information.player_name`. Kalshi market tickers use these display names. `is_default=1` marks the most-recently-active username per player.

### Kalshi market tickers

Two event ticker patterns, defined in `src/kalshi_tt.py`:
- `KXTITLEDTUESDAY-{date}-{PLAYER}` — winner market (N=1)
- `KXTITLEDTUESTOP-{date}T{N}-{PLAYER}` — top-N markets (N=3, 5, 8)

`{date}` is in Kalshi's `YYMONDD` format (e.g. `26SEP16`), produced by `_to_kalshi_date()` from `kalshi_core`. Ticker parsing in `trading.py` extracts N and player name from these patterns.

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
- **Local:** nothing to configure — `DB_PATH = data/titled_tuesday.db` (SQLite, auto-created).
- **Railway-like local test:** put `DATABASE_URL=postgres://...` in `.env.local` (gitignored); `src/db.py` auto-loads it.
