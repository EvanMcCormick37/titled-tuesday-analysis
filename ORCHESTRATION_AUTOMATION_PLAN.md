# Titled Tuesday — Orchestration & Remote-Control Plan

**Goal:** Run the full weekly pipeline on a remote host while the user is traveling. Fully automate the *data layer* (scrape → conflicts → predictions). Keep the *trading layer* (place / take / cancel) behind an explicit, phone-triggered manual action. Expose everything through a Streamlit dashboard + Telegram bot.

**Core principle:** no money moves without an explicit tap from the user. The system's job is to prepare high-quality predictions, surface them cleanly on a phone, and make trading actions one-tap reliable.

---

## 1. Current workflow → target workflow

| Step | Today (manual, laptop) | Target (remote, phone-friendly) |
|---|---|---|
| **Data pipeline** (`scrape_tt` → `update_broadcasts` → `run_raw`) | Three scripts, run manually in sequence | **Single automated pipeline** — one Railway cron, Tue 15:00 ET, immediately after tournament end. Steps run sequentially; a failure halts the pipeline and alerts the user. |
| Fallback when chess.com listing hasn't linked the TT yet | Manually run `update_titled_tuesday.py <slug>` | **Dashboard + Telegram prompt** on scrape failure — paste the tournament URL, the system extracts the slug, resumes the pipeline from where it halted. |
| Edit `CUT_PLAYERS`, `KEEP_PLAYERS`, `P_OVERRIDES`, `P_NUDGES` | Edit notebook cell | **Dashboard form** writing to a new `manual_adjustments` DB table. |
| Adjusted predictions | `run_adjusted()` in notebook | **Triggered by "Save & recompute"** on the dashboard. No cron — the user drives this step on their own cadence. |
| Visualize odds | Matplotlib plots in notebook | **Streamlit dashboard** (mobile-responsive). |
| `place_bids.py` | `python scripts/place_bids.py --live` | **Dashboard button + Telegram `/place`** (confirm → execute). |
| `take_trades.py` | `python scripts/take_trades.py --live --loop` | **Dashboard button + Telegram `/take`** (one-shot or loop toggle). |
| `cancel_bids.py` | `python scripts/cancel_bids.py --live` | **Dashboard button + Telegram `/cancel`** (failsafe). |
| Monitor positions / orders | Notebook cells + Kalshi web UI | **Dashboard "Portfolio" tab** (live from Kalshi API). |

---

## 2. Target architecture (Railway)

Railway supplies the three primitives we need: **web services**, **cron jobs**, and **managed Postgres / Volumes**. No Linux VM is required.

Services (all three share the same Docker image, differing only in their start command):

- **`tt-app`** (web service, long-running): Streamlit dashboard. Also hosts the in-process pipeline runner — when the user taps "Retry pipeline" or "Save & recompute", the function runs in a background thread of this service and writes state to Postgres as it goes. UI polls DB for state.
- **`tt-bot`** (worker service, long-running): Telegram bot (long-poll). Reads state from Postgres; for trading actions, calls into the same `src.trading` functions as the dashboard.
- **`tt-cron`** (Railway Cron, scheduled): fires once a week — `python scripts/weekly_pipeline.py` — Tue 15:00 ET. Nothing else is on a schedule.

```
┌────────────────────────────────────────────────────────────────┐
│  Railway project: titled-tuesday                               │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ tt-cron (Railway Cron — once/week, Tue 15:00 ET)        │   │
│  │   python scripts/weekly_pipeline.py                     │   │
│  │     ↳ scrape_tt → update_broadcasts → run_raw           │   │
│  │     ↳ halt + alert on any failure                       │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │                                    │
│                           ▼                                    │
│  ┌─────────────────────────────────────┐                       │
│  │ Postgres (Railway-managed)          │                       │
│  │  · tt_standings, tt_tournaments     │                       │
│  │  · player_information               │                       │
│  │  · latest_model_predictions(_raw)   │                       │
│  │  · manual_adjustments  (new)        │                       │
│  │  · pipeline_runs       (new)        │                       │
│  │  · job_runs            (new)        │                       │
│  │  · attendance_conflicts, …          │                       │
│  └──┬────────────────────────────────┬─┘                       │
│     │                                │                         │
│     ▼                                ▼                         │
│  ┌────────────────┐              ┌────────────────┐            │
│  │ tt-app         │              │ tt-bot         │ ◄── phone  │
│  │ (Streamlit)    │ ◄── phone    │ (Telegram      │   Telegram │
│  │                │   browser    │   long-poll)   │            │
│  │ · odds charts  │              │                │            │
│  │ · adjustments  │              │ · alerts       │            │
│  │ · retry/resume │              │ · /place /take │            │
│  │   pipeline     │              │   /cancel      │            │
│  │ · trade buttons│              │ · /retry [url] │            │
│  └────────────────┘              └────────────────┘            │
└────────────────────────────────────────────────────────────────┘
          ▲                                       ▲
          │ outbound HTTPS                        │ outbound HTTPS
          ▼                                       ▼
   chess.com + lichess                     Kalshi API
                                           (creds from Railway env vars)
```

### Why Railway instead of a bare VM
- You already pay for it, and you want to expand to additional Kalshi markets later. Railway's project/service model makes "add another market" = "add another service", not "stand up another VM".
- Managed Postgres removes the single biggest footgun of a travel setup: SQLite on a server you can't shell into.
- Built-in cron, zero-ops TLS, GitHub-connected deploys.

### What Railway *doesn't* give you (and the mitigations)
- **No persistent file system across restarts** (unless you attach a Volume). → Move state to Postgres; attach a Volume only for the Lichess PGN dumps cache if we decide re-downloading them is too expensive.
- **No native "trigger this on button-press" primitive**. → The dashboard calls the trading scripts in-process (they're already library functions in `src/trading.py` — just import and call).
- **No GPU, modest CPU.** → Confirmed non-issue: `run_raw()` at 100k sims is ~60s on a laptop; Railway Hobby instances have enough CPU. If it ever bites, drop to 50k sims for the automated runs and only use 100k when the user hits "Recompute".

---

## 3. Service-by-service responsibilities

### 3.1 Weekly data pipeline — one function, one schedule, resumable

The three pre-trade scripts (`update_titled_tuesday`, `update_broadcasts`, `run_raw`) are a single sequential workflow: each step depends on the previous one, and a failure anywhere makes everything downstream stale. We model them as one function — `scripts/weekly_pipeline.py:run_pipeline()` — that walks a small state machine, persisting progress to the `pipeline_runs` table so a failure can be resumed from the exact step that broke.

**Steps (strict order):**

```
scrape_tt → update_broadcasts → run_raw → ADJUSTMENTS_PENDING
                                              ↑
                                       user-driven loop:
                                       edit adjustments → run_adjusted
                                              ↓
                                           (next cycle)
```

**What each step actually does:**

- **`scrape_tt`** — hits chess.com's completed-TT listing, discovers any new slug(s) since last run, scrapes standings for each, upserts `tt_standings` / `tt_tournaments`, enriches new usernames via the chess.com PubAPI.
- **`update_broadcasts`** — streams the *current month*'s Lichess PGN dump (and the previous month's dump during the first ~week of each new month, since Lichess finalises last month a few days late) through the headers-only parser into memory, upserts `other_events` / `other_event_rounds` / `other_event_participants`, then rebuilds `attendance_conflicts`. **No filesystem cache.** The DB is the single source of truth for historical broadcast events — rows older than two months are never re-scraped by the weekly pipeline. (The existing `data/lichess_dumps/` cache gets deleted in Phase 0; the one-time 2020→present backfill lives in Postgres as of migration.)
- **`run_raw`** — loads prepared standings via `src.data.load_and_prepare()`, builds the score-composite player pool, runs the 100k MC simulation, writes `latest_model_predictions_raw`. ~60s on a Hobby-tier Railway instance.

`run_adjusted` is **not** part of the pipeline. It's a user-driven step that fires whenever adjustments are saved on the dashboard. The pipeline halts after `run_raw` with state = `ADJUSTMENTS_PENDING` and waits.

**Trigger sources** (all call the same `run_pipeline(resume_from=..., slug_override=...)` function):

1. **Railway Cron** — Tue 15:00 ET, immediately after the tournament's typical end time. No kwargs.
2. **Dashboard "Retry pipeline" button** — appears when the current `pipeline_runs` row is in a failed state. Resumes from the failed step.
3. **Dashboard "Scrape with URL" form** — appears specifically when the scrape step failed because chess.com's listing page hasn't surfaced the TT yet. User pastes the direct tournament URL; the backend extracts the slug via the existing `TT_SLUG_RE` and resumes.
4. **Telegram `/retry` command** — same as the dashboard button; takes an optional URL arg.

**Failure handling for each step:**

| Step | Typical failure mode | What the system does |
|---|---|---|
| `scrape_tt` (listing scan) | Previous Tuesday's TT not yet linked from the listings page | Mark pipeline `failed` with `failed_step = 'scrape_tt'`, `error_kind = 'listing_missing_slug'`. Alert prompts user for direct URL. |
| `scrape_tt` (page fetch) | chess.com 5xx or rate-limit | Mark `failed`, `error_kind = 'http_error'`. Alert "Retry" (no URL needed — assume the slug *is* discoverable, just transient). |
| `update_broadcasts` | Lichess 5xx on PGN dump download | Mark `failed`, `error_kind = 'http_error'`. Alert "Retry". |
| `run_raw` | OOM, DB corruption, pool-build math error | Mark `failed`, `error_kind = 'computation_error'`. Alert with first line of traceback. |

**URL-override path for `scrape_tt`:**

The existing `update_titled_tuesday.py` already supports `process(slug)` as a direct call. The pipeline step wraps it like this:

```python
def step_scrape_tt(slug_override: str | None = None) -> dict:
    if slug_override:
        slug = _slug_from_url(slug_override)   # strip https://..., validate regex
        process(slug)
        return {"mode": "override", "slug": slug}
    known = known_slugs()
    new = [s for s in discover_slugs() if s not in known]
    if not new:
        raise ListingMissingSlugError("Chess.com listing has no new TT slug yet.")
    for slug in new:
        process(slug)
    return {"mode": "listing", "slugs": new}
```

The `ListingMissingSlugError` is the signal that the dashboard should show the URL input form rather than a generic "Retry" button.

### 3.2 User-driven step — `run_adjusted`

After the pipeline halts in `ADJUSTMENTS_PENDING`, the user edits the attendance adjustments on the dashboard (Attendance tab). The **"Save & recompute"** button:

1. Writes/updates rows in `manual_adjustments` for the upcoming `tourn_date`.
2. Fires `run_adjusted(tourn_date)` in a background thread; function reads `manual_adjustments` via the new `pipeline.load_adjustments()` helper and writes `latest_model_predictions`.
3. UI shows a progress spinner (adjusted MC run is ~60s at 100k sims), then refreshes the Odds tab.
4. On success, moves `pipeline_runs.state → 'completed'`.

The user can hit "Save & recompute" as many times as they like during the week — each run overwrites `latest_model_predictions` and a corresponding `job_runs` row records the attempt. The last successful recompute wins.

If the user never touches the dashboard that week, `pipeline_runs.state` stays `ADJUSTMENTS_PENDING` and `latest_model_predictions` still holds the previous week's result. That's fine — but the bot pings every 24 hours ("You still have unapplied adjustments for 2026-10-07") as a nag, with a quiet-mode toggle.

### 3.3 `tt-app` — Streamlit dashboard

Single-page app with tabs. Hosted at a Railway-provided subdomain. Auth: Streamlit's native password + a strong `DASHBOARD_PASSWORD` env var (phase 1 — can harden later with Cloudflare Access).

**Persistent "Pipeline status" banner** across every tab (so you never miss a halted pipeline when you open the app):

- `✓ Completed — 2026-10-07 run_adjusted ran 2h ago` (green)
- `⏳ Adjustments pending — run_raw completed, waiting for your edits` (yellow)
- `✗ Scrape failed — listing has no new slug yet. [Paste URL]` (red, with inline form)
- `✗ Broadcasts failed — Lichess 503 at 2026-10-07 15:12. [Retry]` (red)

**Tabs:**

1. **Odds** — the `plot_top_players_by_chance` / `plot_yes_no_prices` / `plot_ip_advantage` plots, plus the top-N tables from `latest_model_predictions`. Tap a player to drill into their history. Toggle raw ↔ adjusted. Mobile-first layout (one plot per screen, swipe-friendly).

2. **Attendance Adjustments** — the critical weekly input surface. Replaces the top cell of `command-center.ipynb`:
   - Form fields: Player (autocomplete over `player_information.player_name`), Adjustment type (`cut`, `keep`, `override`, `nudge`), Value (if `override` or `nudge`), optional note.
   - "Global nudge" slider.
   - Table of current adjustments for the upcoming tourn_date with delete buttons.
   - "Save & recompute" button → writes to `manual_adjustments`, triggers `run_adjusted` in a background thread, polls `job_runs` to show live progress, re-renders Odds tab on completion.

3. **Pipeline** — the detailed view of the current and recent `pipeline_runs`. Steps shown as a vertical timeline with status dots. Buttons appropriate to the current state:
   - `ADJUSTMENTS_PENDING` → "Save & recompute" lives here too (duplicate of the Attendance tab button, since this is where someone goes when the banner says "pending").
   - `FAILED @ scrape_tt / listing_missing_slug` → text input + "Scrape with this URL".
   - `FAILED @ scrape_tt / http_error` or `FAILED @ update_broadcasts / http_error` → "Retry step".
   - `FAILED @ run_raw / computation_error` → "Retry" + a link to the error details.

4. **Portfolio** — current Kalshi positions via `get_tt_positions_df`, the P&L simulation from `run_portfolio_mc_score`, and the Kelly sizing block. Live-refresh button.

5. **Trade** — three primary actions, each behind a confirm-dialog. Disabled with a tooltip when `pipeline_runs.state != 'completed'` (don't trade on stale adjusted predictions). All form defaults match the current CLI scripts (notably `count=200`), so dashboard behavior is a pure port of the user's existing habits:
   - **Place bids** — `src.trading.place_bids(dry_run=True)` runs first and renders the plan; a second tap executes with `dry_run=False`. Form controls for `count` (default 200), `markup` (default 1.5), `max_discount` (default 15), `side_filter`.
   - **Take trades** — `src.trading.take_trades(dry_run=True)` → plan preview → confirm. Form controls for `min_roi` (default 1.5), `min_qty` (default 25), `max_qty` (default 200), `best_per_event` (default on), optional `budget`.
   - **Cancel bids** — fetches current TT resting orders, shows them with checkboxes (default all-selected), one-tap cancel.

6. **Jobs** — recent `job_runs` rows (job, started, duration, status, summary). Tap a row for full log output.

### 3.4 `tt-bot` — push + command surface

For when you don't want to open a browser (slow airport wifi, between gates, in bed). Uses `python-telegram-bot`. Chat is private, bound to your `TELEGRAM_CHAT_ID`.

**Push alerts (bot → you):**
- `[pipeline] 2026-10-07 ✓ scrape_tt → ✓ broadcasts → ✓ run_raw (6,977 players, 58s). Adjustments pending.`
- `[pipeline] 2026-10-07 ✗ scrape_tt — chess.com listing has no new TT slug yet. Reply /retry <url>.`
- `[pipeline] 2026-10-07 ✗ update_broadcasts — Lichess 503. Reply /retry.`
- `[run_adjusted] ✓ 7 adjustments applied, top-1 changed by Nakamura (+4.2pp).`
- `[trade] Placed 24 bids / cancelled 6 — exposure $87.40.`

**Commands (you → bot):**

| Command | Behavior |
|---|---|
| `/status` | One-line summary of current `pipeline_runs` state + `latest_model_predictions.tourn_date`. |
| `/retry [url]` | Resume the halted pipeline. With a URL arg, pass through to `scrape_tt` as a slug override. |
| `/odds <player>` | Prints `p_participate`, `P_top{1,3,5,8}` and fair YES/NO prices. |
| `/top <N>` | Prints top-10 players by `P_top{N}`. |
| `/conflicts` | Lists current adjustments for upcoming tourn_date. |
| `/cut <player>` | Adds a `cut` adjustment (confirm reply). |
| `/keep <player>` | Adds a `keep` adjustment. |
| `/override <player> <prob>` | Sets `p_participate`. |
| `/recompute` | Fires `run_adjusted`. |
| `/positions` | Current Kalshi positions. |
| `/orders` | Open TT orders. |
| `/place` | Dry-run `place_bids`, replies with a summary + inline Confirm/Cancel buttons. On Confirm, re-runs live. |
| `/take` | Same pattern for `take_trades`. |
| `/cancel` | Lists resting orders + inline "Cancel all" button. |

All destructive commands require a confirm button tap. The bot never acts on a bare slash command. Trade commands refuse to execute when `pipeline_runs.state != 'completed'` (same stale-data guard as the dashboard).

---

## 4. Database changes

Two new tables, one data migration.

### `manual_adjustments` (new)
```sql
CREATE TABLE manual_adjustments (
    id              INTEGER PRIMARY KEY,
    tourn_date      DATE NOT NULL,
    player_name     TEXT NOT NULL,            -- resolves via player_information
    adjustment_type TEXT NOT NULL             -- 'cut' | 'keep' | 'override' | 'nudge' | 'global_nudge'
                    CHECK (adjustment_type IN ('cut','keep','override','nudge','global_nudge')),
    value           REAL,                     -- NULL for 'cut'/'keep', required otherwise
    note            TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    created_by      TEXT                      -- 'dashboard' | 'telegram' | 'cli'
);
CREATE INDEX idx_adj_tourn ON manual_adjustments(tourn_date);
```

`global_nudge` rows use `player_name = ''` as a sentinel.

### `pipeline_runs` (new) — one row per weekly cycle
```sql
CREATE TABLE pipeline_runs (
    id              SERIAL PRIMARY KEY,
    tourn_date      DATE NOT NULL UNIQUE,     -- the TT the raw data is for (not the upcoming tourn)
    state           TEXT NOT NULL             -- 'running' | 'adjustments_pending' | 'completed' | 'failed'
                    CHECK (state IN ('running','adjustments_pending','completed','failed')),
    current_step    TEXT,                     -- 'scrape_tt' | 'update_broadcasts' | 'run_raw' | 'run_adjusted' | NULL
    failed_step     TEXT,                     -- same vocabulary, non-NULL iff state='failed'
    error_kind      TEXT,                     -- 'listing_missing_slug' | 'http_error' | 'computation_error' | NULL
    error_message   TEXT,
    started_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
```

`tourn_date` here means "the Tuesday whose results we just scraped" — e.g. a 2026-10-07 15:00 ET pipeline run produces `tourn_date = '2026-10-07'`, and the upcoming tournament to predict is one week later. Having `tourn_date` as `UNIQUE` means re-running the pipeline for the same TT updates the row in place (`ON CONFLICT`).

### `job_runs` (new) — fine-grained log of every step/action
```sql
CREATE TABLE job_runs (
    id               SERIAL PRIMARY KEY,
    pipeline_run_id  INTEGER REFERENCES pipeline_runs(id),   -- NULL for ad-hoc actions (place_bids, etc.)
    job_name         TEXT NOT NULL,            -- 'scrape_tt' | 'update_broadcasts' | 'run_raw' | 'run_adjusted' | 'place_bids' | 'take_trades' | 'cancel_bids'
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ,
    status           TEXT NOT NULL             -- 'running' | 'success' | 'failed'
                     CHECK (status IN ('running','success','failed')),
    summary          JSONB,                    -- small json payload (counts, errors)
    log_tail         TEXT                      -- last ~16KB of stdout for debugging
);
CREATE INDEX idx_jobs_pipeline ON job_runs(pipeline_run_id);
```

`job_runs` captures each individual invocation; `pipeline_runs` is the higher-level state machine for the weekly cycle. Many `job_runs` rows can roll up to one `pipeline_runs` row (e.g. a scrape that failed, was retried with a URL override, then succeeded = two `job_runs` rows for `scrape_tt`).

### Change to `pipeline.run_adjusted()`

Today it takes `cut_players`, `keep_players`, `p_participate_overrides`, `p_nudges`, `global_nudge` as kwargs. Add a new helper `load_adjustments(tourn_date) -> dict` that reads from `manual_adjustments` and returns the same dicts, and change `make_predictions.py` (and the scheduler) to call it. The notebook keeps working — it still accepts kwargs — but the production path reads from the DB.

### SQLite → Postgres migration

- Export `data/titled_tuesday.db` with `sqlite3 .dump`, massage types (TEXT datetimes → TIMESTAMPTZ, `0`/`1` INTEGER booleans → BOOLEAN), load into Railway Postgres.
- Replace all `sqlite3.connect(DB_PATH)` sites (there are ~15, mostly in `src/pipeline.py`, `src/data.py`, `src/trading.py`, and the scraping scripts) with `psycopg[binary]` + a `get_conn()` helper reading `$DATABASE_URL`.
- Replace `pd.read_sql_query` calls — those work identically against psycopg.
- `df.to_sql(..., if_exists='replace')` → use `TRUNCATE + COPY` for speed; or a small `upsert_df` helper.
- Keep the SQLite path as a fallback via a `DB_BACKEND` env var — useful for local dev and for the backtest notebook which does heavy reads.

**Alternative if the migration feels heavy:** keep SQLite on a Railway Volume, mount it in both services, accept that concurrent writes from the dashboard + scheduler + bot need a `BEGIN IMMEDIATE` wrapper. Workable, but Postgres is strictly better for the "expand to more markets" direction.

---

## 5. Secrets & credentials

Railway env vars for each service:

```
# Shared
DATABASE_URL=postgres://...                   # provided by Railway Postgres plugin
KALSHI_API_KEY_ID=<from ~/.kalshi/.env>
KALSHI_PRIVATE_KEY_B64=<base64 of trading-key.txt>
KALSHI_ENV=prod

# Dashboard only
DASHBOARD_PASSWORD=<long random>

# Bot only
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHAT_ID=<your private chat id>
```

`kalshi_core.KalshiClient` currently reads from `~/.kalshi/`. Smallest-blast-radius change: add a `KalshiClient.from_env()` constructor that reads the three env vars, decoding the base64 private key to a tempfile at process start. Keep the file-based loader intact for local dev.

---

## 6. `kalshi-core` sibling repo

The project imports `kalshi_core` as an editable local install from `../kalshi-core`. Railway deploys only see the current repo. Options:

1. **Git-ref install** (recommended) — `pip install git+https://${GH_TOKEN}@github.com/EvanMcCormick37/kalshi-core.git@main`. `GH_TOKEN` is a Railway env var. Simple, keeps the two repos independent.
2. Vendor `kalshi-core` into this repo as a git subtree. Zero deploy friction but tightens the coupling.
3. Publish `kalshi-core` to a private PyPI index. Overkill.

Go with option 1 unless there's a reason the kalshi-core repo is particularly volatile.

---

## 7. Weekly timeline from the user's POV

```
Tue 14:00 ET  tournament ends
         ↓
Tue 15:00 ET  (automated) weekly_pipeline fires
                step 1  scrape_tt         ─┐
                step 2  update_broadcasts  ├─ any failure → HALT + Telegram alert
                step 3  run_raw           ─┘
         ↓
              state: ADJUSTMENTS_PENDING
              Telegram: "Raw ready for 2026-10-14. Review on dashboard."
         ↓
(user, any time between now and next Tue tournament)
         ↓
              · open dashboard → Attendance tab
              · add/edit conflicts (cuts, keeps, overrides, nudges)
              · tap "Save & recompute"
                  ↳ writes manual_adjustments
                  ↳ fires run_adjusted (~60s, progress spinner)
                  ↳ writes latest_model_predictions
                  ↳ pipeline_runs.state → 'completed'
              · can repeat as many times as desired
         ↓
Next Tue 08:00-10:50 ET  (user, trading window)
              · /odds / /top to spot-check
              · tap "Place bids" on dashboard (or /place in bot) — confirm → live
              · tap "Take trades" when a juicy ask appears
              · /cancel as needed if a surprise lineup change hits
         ↓
Next Tue 11:00 ET  tournament starts
Next Tue 14:00 ET  tournament ends
Next Tue 15:00 ET  pipeline fires again — cycle repeats
```

**Failure branch — chess.com hasn't linked the TT yet:**

```
Tue 15:00 ET  (automated) weekly_pipeline fires
                step 1  scrape_tt → ListingMissingSlugError
                                     ↓
              state: FAILED @ scrape_tt / listing_missing_slug
              Telegram: "Scrape failed — no new slug on listing. Reply /retry <url>."
         ↓
(user, phone)  open chess.com in browser, find the TT page,
               copy URL, paste into dashboard "Scrape with URL" form
               (or send /retry https://chess.com/tournament/live/… to the bot)
         ↓
         pipeline resumes: scrape_tt (with slug override) → update_broadcasts → run_raw
         ↓
         state: ADJUSTMENTS_PENDING   (continues as normal)
```

Every pipeline step emits a Telegram success/failure line. Nothing hits Kalshi without a tap.

---

## 8. Observability & failure handling

- **Every job writes a `job_runs` row.** `started_at` on entry; `ended_at` + `status` + `summary` + `log_tail` on exit. On uncaught exception: status = `failed`, exception traceback in `log_tail`.
- **Every weekly cycle writes a `pipeline_runs` row** that tracks the state machine (`running` → `adjustments_pending` → `completed`, or any → `failed`). The dashboard's persistent status banner reads from here.
- **Telegram is the primary alert channel.** Success alerts are terse; failure alerts include the `error_kind`, the exception's first line, and the specific action required (paste URL, tap /retry, etc.).
- **In-step retries:** `scrape_tt` and `update_broadcasts` wrap their HTTP calls in a 3-attempt retry with exponential backoff before giving up and transitioning to `failed`. The pipeline-level "Retry step" button is for the case where even those retries exhausted.
- **Idempotency:** every step is safe to re-run. `scrape_tt`'s `process(slug)` already deletes-and-reinserts standings for that slug. `update_broadcasts` is `INSERT OR REPLACE`. `run_raw` writes to `latest_model_predictions_raw` with `if_exists='replace'`. So "Retry step" never corrupts data.
- **Stale-data guard on trades:** Trade actions (dashboard + bot) refuse to execute unless `pipeline_runs` for the most recent Tuesday is in `completed` state. If the user wants to force-trade on stale data anyway (e.g. network between them and the bot is flaky), there's an "I know, run anyway" checkbox in the dashboard's trade confirm dialog.
- **Nag timer:** if `pipeline_runs.state = 'adjustments_pending'` for >24h and we're <36h from the next tournament, bot sends a reminder. Can be silenced per-week with `/mute`.
- **Nightly Postgres backup:** Railway has native snapshots, but add a `pg_dump → S3` daily cron for belt-and-braces. (Phase 5.)

---

## 9. Phased rollout

Each phase is independently useful; stop any time if the next one isn't worth the effort.

### Phase 0 — Local groundwork (no Railway yet)
Goal: get the current project ready to run in a container without any manual notebook interaction.
- Add `manual_adjustments` + `pipeline_runs` + `job_runs` tables (SQLite migration script).
- Add `pipeline.load_adjustments(tourn_date)`; refactor `make_predictions.py` to use it. Notebook keeps working.
- Add `scripts/weekly_pipeline.py` with `run_pipeline(resume_from=None, slug_override=None)` driving the state machine. Each step wraps `job_runs` logging; `pipeline_runs` is updated on step transitions.
- Add `scripts/trigger_adjusted.py` as the on-demand entry point for `run_adjusted` (called by dashboard + bot).
- Extract `ListingMissingSlugError` from `update_titled_tuesday.discover_slugs()` and plumb `_slug_from_url(url)` as a tiny utility.
- Rewrite `update_broadcasts.py` to stream PGN dumps straight through the parser into Postgres with no filesystem cache. Delete `data/lichess_dumps/` and remove the `--refresh-recent` / `--full-rebuild` flags (dead with no cache).
- `KalshiClient.from_env()` constructor.
- `Dockerfile` + `pip install git+...kalshi-core`. Test container locally end-to-end against local SQLite: happy path, listing-missing-slug fallback, http-retry fallback.

### Phase 1 — Railway: pipeline + Postgres
- Spin up Railway project, add Postgres plugin.
- One-shot DB cutover: `sqlite3 .dump` → massage types → `psql`. Verify row counts match for every table. Archive the SQLite file (`data/titled_tuesday.db` + WAL) as `data/titled_tuesday.pre-pg.YYYY-MM-DD.db` and keep it read-only on disk for ~4 weeks as an insurance rollback target. Not pushed to Railway after cutover.
- Deploy `tt-cron` service (same image, start command = `python scripts/weekly_pipeline.py`), wire up a single Railway Cron entry at Tue 15:00 ET.
- Deploy a minimal `tt-bot` with just alert pushes (not commands yet) so you can see the pipeline running green.
- Verify two consecutive weeks run green with zero intervention — and deliberately trigger the listing-missing-slug branch once to confirm the retry path works end-to-end.

### Phase 2 — Streamlit dashboard
- Deploy `tt-dashboard`.
- Build Odds + Attendance Adjustments tabs first — those are the ones that replace the notebook workflow and are the hardest to live without on the road.
- Portfolio + Jobs tabs next.

### Phase 3 — Trade controls (still manual)
- Add Trade tab with place / take / cancel buttons.
- Wire two-tap confirm (dry-run → live).
- First production use: do a tiny-size bid (count=1) week to shake out bugs before trusting it with real exposure.

### Phase 4 — Telegram bot
- Push alerts for every scheduler job.
- Read-only commands first (`/odds`, `/top`, `/conflicts`, `/positions`, `/orders`).
- Write commands last (`/place`, `/take`, `/cancel`), with inline confirm buttons.

### Phase 5 — Hardening
- Nightly `pg_dump` to S3 (or Railway's native backup if sufficient).
- Rate-limit the bot's write commands.
- Replace Streamlit password with Cloudflare Access or Auth0.
- Alert on "data looks weird" conditions: pool size drops >10% week-over-week, >100 unmatched player names, etc.

---

## 10. Open questions / things to revisit

- **Multi-market expansion.** Confirmed as a design driver. The cleanest expansion path:
  - Reorganise this repo into `markets/titled_tuesday/` as a self-contained package: its own `src/`, `scripts/weekly_pipeline.py`, `src/trading.py`. Any new Kalshi market gets a sibling `markets/<slug>/` directory.
  - Each market gets its own Railway Cron service (`tt-cron`, `<market>-cron`, …) running that market's own `weekly_pipeline.py`.
  - **Shared** across all markets: Postgres (schemas or table prefixes namespace per-market data), the Streamlit dashboard (gains a market picker in the top nav), the Telegram bot (commands take an optional market arg; default = TT until a second market exists).
  - `kalshi_core` keeps its current scope: API client + generic math + account-level `fills` / `portfolio_snapshots`. Market-specific logic (ticker patterns, name mapping, simulation models) stays per-market.
  - Worth sketching the `markets/<slug>/` package layout **before Phase 2** so the dashboard code paths aren't painted into a TT-only corner. Adding a second market is a Phase 6+ concern, but the two-week cost of refactoring now vs. refactoring later is wildly different.

Resolved from the previous revision:
- *Lichess PGN cache* — dropped. No filesystem cache; DB is the only source of truth; weekly pipeline only re-scrapes current/previous month.
- *Pipeline run slot* — Tue 15:00 ET is locked. Tournament ends ~13:15 ET and results are fully posted by 15:00. No late session exists post-Sep 2025.
- *Backtest / notebooks on the road* — out of scope. Backtesting and analysis stay on the laptop; the remote system automates core trading-week functionality only.
- *Single-service alternative* — rejected. Three-service design is a go.
