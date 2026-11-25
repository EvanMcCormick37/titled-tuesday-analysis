# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@../kalshi-core/CLAUDE.md

## Project Purpose

Production Python trading system for betting on chess.com's weekly **Titled Tuesday** blitz tournament, **Bullet Brawl** 1+0 arena and **Freestyle Friday** chess960 Swiss using Kalshi prediction markets. The system:
1. Scrapes historical standings from chess.com + concurrent-event broadcast data from Lichess
2. Predicts each player's probability of attending and finishing in the top N (Monte Carlo)
3. Places and executes orders on Kalshi using those fair-value estimates

Production runs on **Railway**: several services sharing one Docker image and one Postgres instance. Local development runs against SQLite through the dual-backend DB layer.

## How the docs are organized

| Where | What | Shelf life |
|---|---|---|
| This file | Deployment, commands, cross-cutting rules, the map below | Current truth |
| `src/CLAUDE.md`, `src/mm/CLAUDE.md`, `src/research/CLAUDE.md`, `scripts/scraping/CLAUDE.md` | One subsystem each; Claude Code loads them when you work in that directory | Current truth |
| `docs/decisions/` | Dated records of *why* something was decided (test results, rejected options). Never edited after the fact; a later record supersedes an earlier one. | Historical, by design |
| `docs/proposals/` | Plans not yet built, each with a status line at the top | Temporary |

Rules:
- **A change that alters documented behaviour updates the relevant `CLAUDE.md` in the same commit.**
- `CLAUDE.md` files describe only what exists now. Plans go in `docs/proposals/`. When a proposal ships, move what was built into the right `CLAUDE.md`, record the main reasoning in `docs/decisions/` (one file per decision, `<yyyy-mm-dd>-<short-name>.md`), and delete the proposal.
- A new subsystem `CLAUDE.md` must be added to the table above. `tests/test_docs.py` checks that, and that every repo path named in backticks in these files exists.

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
| `tt-market-maker` | `python scripts/market_maker.py` | Reactive two-tier market maker for every registered market (`src/mm/CLAUDE.md`). Shadow unless `MM_LIVE=1`. Run exactly one replica; restart policy ON_FAILURE. The code calls it `tt-mm`; tt-worker finds it by its DB heartbeat, so the service name doesn't matter |

**Important Railway gotcha:** Cron expressions are evaluated in UTC regardless of the `TZ` env var. Use numeric day-of-week (`0` = Sunday, `2` = Tuesday, `6` = Saturday), not the `TUE` alias. `TZ=America/New_York` only affects the container's clock inside Python (which matters for `MarketSpec.most_recent_event()`, the date each pipeline run is for).

Env vars (set in Railway UI, per service unless noted):
- **Shared:** `DATABASE_URL` (always the reference `${{Postgres.DATABASE_URL}}`, never a pasted URL: a copied public URL goes stale when the password changes, which crashed tt-worker and tt-cron in 2026-11), `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_B64` (base64 of `~/.kalshi/trading-key.txt`), `KALSHI_ENV=prod`, `TZ=America/New_York`, `GH_TOKEN` (PAT for installing private `kalshi-core`)
- **tt-api and tt-app:** `DASHBOARD_PASSWORD`
- **tt-worker only:** `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30`
- **tt-market-maker only:** `MM_LIVE=1` to send orders (otherwise shadow), optional `MM_SCOUTS=0` (anchors only), `MM_MARKETS` (comma-separated slugs), `MM_JOURNAL_DIR` (default `data/mm_journal`; mount a volume there to keep journals across deploys). Needs a kalshi-core build that includes `KalshiWS`, so push kalshi-core `main` before deploying

**Schema changes are not applied on deploy.** Before deploying code that needs new columns or tables, run the migration against Railway, e.g. `railway ssh` into tt-worker and run `python scripts/migrate_multi_market.py` (it uses the internal `DATABASE_URL`), or from your machine with `DATABASE_URL='<public URL>' python scripts/...` set for that one command. Only the tables created by `job_queue.init_schema()` (queue, `mm_*`, `research_*`) are created automatically, by tt-api and tt-worker at startup.

The `Dockerfile` installs `kalshi-core` at build time from a private GitHub repo using `GH_TOKEN`. Local dev can bind-mount `/opt/kalshi-core` instead — `docker-entrypoint.sh` handles both paths.

## Common Commands

Run everything from the `tt` conda env (see "Local analysis").

```bash
pip install -r requirements.txt

# Full weekly pipeline (scrape → broadcasts → raw MC → research → adjustments_pending)
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
cd web && npm run dev                       # frontend, with the API on port 8000

# Market maker
python scripts/market_maker.py              # shadow: live books, computes quotes, sends nothing
MM_LIVE=1 python scripts/market_maker.py    # live

# Tests
python -m pytest tests                      # fake Kalshi client; TEST_PG_URL=<disposable Postgres> also runs against Postgres (tables are dropped)
```

Local dashboard: `DASHBOARD_PASSWORD` is required (login fails closed); set `TT_INSECURE_COOKIE=1` for plain-http testing.

## Architecture map

| Area | Where | Details |
|---|---|---|
| Weekly pipeline | `scripts/weekly_pipeline.py`, `src/pipeline.py` | Below |
| Scrapers | `scripts/scraping/` | `scripts/scraping/CLAUDE.md` |
| Model, data, player names | `src/` | `src/CLAUDE.md` |
| Trade queue | `src/trade_plan.py`, `src/job_queue.py`, `src/job_runner.py`, `src/strategies.py` | Below |
| Market maker | `src/mm/`, `scripts/market_maker.py` | `src/mm/CLAUDE.md` |
| Attendance researcher | `src/research/` | `src/research/CLAUDE.md` |
| Dashboard | `api/main.py` + `web/` | FastAPI + React for every registered market (market picker in the top bar); pages include Trade, Market maker, Edges, Portfolio, Odds, Attendance, Pipeline, Logs. Gated by `DASHBOARD_PASSWORD`. Legacy Streamlit: `app/dashboard.py` (Titled Tuesday only) |

### Weekly pipeline

`scripts/weekly_pipeline.py` runs a market's pre-trade steps (`MarketSpec.pipeline_steps`: `scrape_tt`, `scrape_bb` or `scrape_ff` → `update_broadcasts` → `run_raw` → `research`) as a resumable state machine, writing `pipeline_runs` + `job_runs`. On any failure it halts with `state='failed'` and an `error_kind` the dashboard uses to show the right retry UI. `scripts/trigger_adjusted.py` runs `run_adjusted` from `manual_adjustments`, writes `latest_model_predictions`, and flips the `pipeline_runs` row to `completed`.

```
         [cron fires: TT Tuesday evening; FF and BB the morning after]
                 ↓
         scrape_tt / scrape_bb / scrape_ff
         │   listing_missing_slug → state=failed  (user pastes the URL on the Pipeline page)
         │   http_error           → state=failed  (user hits Retry pipeline)
         ↓
         update_broadcasts
         │   http_error           → state=failed
         ↓
         run_raw
         │   computation_error    → state=failed
         ↓
         research  (advisory; never fails the run)
         ↓
         state = adjustments_pending  (user opens the Attendance page, edits manual_adjustments,
                 ↓                      taps "Save & recompute")
         run_adjusted
                 ↓
         state = completed  (trading unlocks)
```

Each run is for `spec.most_recent_event()` and is keyed (market, tourn_date). If the cron never started (no row for the latest event once its day has passed), `/status` reports `pipeline_missing` and the dashboard shows a **Run pipeline for <date>** button. Runs go through the trade queue, so tt-worker must be up. Spending jobs refuse to fire when the prior event's `pipeline_runs.state != 'completed'` (stale-data guard), unless submitted with the override.

### Trade queue

All dashboard trading goes through one Postgres-backed queue. `tt-api` only adds jobs; `tt-worker` is the only process that sends them.

- **`src/trade_plan.py`**: `Plan`, and `execute_plan()`, the single code path that sends orders. Progress goes through an `emit` callback, not `print`.
- **`src/trading.py`**: read-only `plan_*` functions; the older `place_bids`, `place_fade_no_bids`, `take_trades`, `cancel_tt_bids` are print-based wrappers for the CLI and notebook. Fade bids only touch NO orders expiring at or after the event start, so they never cancel the earlier-expiring bids.
- **`src/strategies.py`**: registry of markets, strategies, param schemas, ops jobs, and snapshots. Add a market here. Forms in the UI are generated from the param schemas.
- **`src/job_queue.py`**: `trade_jobs`, `trade_job_events`, `market_snapshots`. Created automatically by `init_schema()` on API and worker start.
- **`src/job_runner.py`**: `JobRunner` and `Worker`.

Rules: jobs **re-plan when they fire**, and a plan above the approved `max_exposure` halts as `needs_review`. Spending strategies also halt if the prior week's `pipeline_runs` row isn't `completed`. One normal-lane job runs per market (enforced by a partial unique index), and recomputes share that lane because they briefly empty the predictions table. `background`-lane jobs (attendance research) run alongside and never block it. The kill switch (`enqueue_kill`) uses the priority lane. It also pauses the market maker for that market, then waits for tt-mm to confirm the pause (`paused_ack`) before sweeping orders; if no confirmation arrives in time it sweeps anyway and sweeps again afterwards. Live jobs are never auto-retried: a job whose worker dies becomes `interrupted`. Unless given `not_after`, a one-shot expires 15 minutes after its intended start and a loop ends at event start. For a loop, `max_exposure` is the total budget.

## Multiple markets

`src/markets/` holds one `MarketSpec` per weekly event: Kalshi series, N values, start and bid-expiry times, event duration, pipeline steps and a `ModelConfig` (`src/CLAUDE.md`). Trading, snapshots, the pipeline runner and the API all take a spec, defaulting to Titled Tuesday. A spec becomes tradable only when it is registered in `MARKETS` in `src/strategies.py`.

- `pipeline_runs`, `manual_adjustments`, `attendance_conflicts`, both latest-predictions tables, `historical_predictions` and `backtest` have a `market` column. **Always filter on it**, and write predictions with `db.replace_market_rows`.
- Each market keeps its raw results in its own table (`titled_tuesday_standings`, `bullet_brawl_standings`, `freestyle_friday_standings`). Freestyle Friday's has Titled Tuesday's columns, so the same score model applies.
- `scripts/migrate_multi_market.py` applies the multi-market schema to an existing database. It is idempotent.
- The Streamlit app and the trading CLI scripts cover Titled Tuesday only; the React dashboard covers every registered market.
- **Adding a market:** a `MarketSpec` (with `pipeline_steps` and `event_url_example`), a scrape function and `PIPELINES` entry in `scripts/weekly_pipeline.py`, registration in `MARKETS`, a backtest profile in `scripts/run_backtest.py`, and a Railway cron service.
- **Usernames keep chess.com's capitalization** in every table, one spelling per account. Never lowercase them; compare case-insensitively when checking whether an account exists (`scripts/scraping/CLAUDE.md`).

### Kalshi market tickers

- `KXTITLEDTUESDAY-{date}-{PLAYER}` / `KXBULLETBRAWL-{date}-{PLAYER}` / `KXFSFRIDAY-{date}-{PLAYER}`: winner market (N=1)
- `KXTITLEDTUESTOP-{date}T{N}-{PLAYER}` / `KXBULLETBRAWLTOP-{date}T{N}-{PLAYER}` / `KXFSFRIDAYTOP-{date}T{N}-{PLAYER}`: top-N markets (N=3, 5, 8)

`{date}` is Kalshi's `YYMONDD` format (e.g. `26SEP16`), from `_to_kalshi_date()` in `kalshi_core`. `MarketSpec.n_from_ticker` extracts N. Kalshi labels contracts with display names; see "Player names" in `src/CLAUDE.md` for the mapping to usernames.

## Database schema

Postgres on Railway (schema in `scripts/init_postgres_schema.py`); SQLite locally (same shape, type-tolerant). Subsystem tables (`mm_*`, `research_*`) are described in their own `CLAUDE.md`.

- **Results:** `titled_tuesday_standings` / `bullet_brawl_standings` / `freestyle_friday_standings` (per-player results; `date` is `TIMESTAMPTZ` on Postgres), `titled_tuesday_tournaments` (metadata), `player_information` (chess.com enrichment; `is_default=1` marks a player's canonical account).
- **Other tournaments:** `other_events` / `other_event_rounds` / `other_event_participants` (past OTB tournaments from Lichess PGN dumps) and `attendance_conflicts` ((market, date, player_name, username) weeks with a broadcast-round conflict, rebuilt by `scripts/scraping/update_broadcasts.py` or `scripts/build_attendance_conflicts.py --market <slug>`; removed from the attendance denominator by `load_and_prepare()`).
- **Predictions:** `latest_model_predictions_raw` (overwritten by each `run_raw`), `latest_model_predictions` (overwritten by each `run_adjusted`), `historical_predictions` (the last official prediction before each event, i.e. the one traded), `backtest` ((market, tourn_date, model, username) walk-forward output with `played` and `actual_rank`).
- **Orchestration:**
  - `pipeline_runs`: one row per (market, tourn_date), UNIQUE, with `state`, `current_step`, `failed_step`, `error_kind`, `error_message`, `started_at`, `completed_at`. `state ∈ {running, adjustments_pending, completed, failed}`, `error_kind ∈ {listing_missing_slug, http_error, computation_error, unknown}`.
  - `job_runs`: every pipeline step (`pipeline_run_id`, `job_name`, status, `summary` JSON, `log_tail`).
  - `manual_adjustments`: (market, tourn_date, player_name, adjustment_type, value, created_at, created_by). `adjustment_type ∈ {cut, keep, override, nudge, global_nudge}`; the dashboard writes `created_by='dashboard'`. Read by `load_adjustments()`.
- **Kalshi** (kalshi-core's shared DB at `~/.kalshi/data/kalshi.db`): `fills` (account-level trades with `fee_cost`), `portfolio_snapshots`.

## Local analysis

Notebooks and backtests run against a local SQLite copy of Railway, in the `tt` conda env (`requirements-dev.txt` plus `kalshi-core`, Jupyter kernel "Python (tt)"). **Run every script from `tt`**: conda `base` has a psycopg without libpq, so anything touching Postgres fails there with "no pq wrapper available".

- `python scripts/pull_snapshot.py` refreshes `data/chess_tournament_data.db` over a read-only connection. Never put the Railway URL in `.env.local`.
- `python scripts/run_backtest.py --notes "..."` records a run in `data/backtests.db` with its settings and git commit. Pulling a snapshot doesn't affect that file. `src/backtest_runs.py` holds the scoring.
- Judge models with `python scripts/score_vip.py <run ids>`: the **VIP Brier score**, on the fixed cohort of traded players in `src/vip_players.py`, against each player's own top-N rate over the previous year (`skill` > 0 beats it), with price and attendance calibration and bootstrap ranges over events. `--drop-conflicts` leaves out week-of conflicted pairs, as manual cuts would. Whole-field log loss is dominated by long shots that are never traded; keep the cohort fixed so no model chooses which players count.
- `notebooks/model-performance-audit.ipynb` audits Titled Tuesday's live predictions since 2026-07-07: calibration, blind spots, manual-override accuracy, Brier scores against backtest models, and betting results.
- `notebooks/bullet-brawl-score-model.ipynb` tests a Bullet Brawl model that splits score into games played × points per game, instead of replaying raw scores.

## Environment setup (local dev)

Kalshi credentials live in `~/.kalshi/` (not in this repo): `.env` (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV`) and `~/.kalshi/trading-key.txt` (RSA private key for API signing). `kalshi_core.KalshiClient()` loads from there; on Railway, `KalshiClient.from_env()` reads `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_B64` instead.

Database:
- **Local:** nothing to configure. `DB_PATH = data/chess_tournament_data.db` (SQLite, auto-created).
- **Postgres local test:** put a local or disposable Postgres `DATABASE_URL=postgres://...` in `.env.local` (gitignored); `src/db.py` auto-loads it without overriding a variable already set. Never Railway's URL.
