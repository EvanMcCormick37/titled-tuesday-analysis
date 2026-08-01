# Titled Tuesday Analysis — Refactor Plan

## Vision

Transform this from a collection of scripts and notebooks into a **weekly command center**: open one notebook on Tuesday, run a few cells, review model predictions with your own overrides applied, see the best Kalshi trades, and eventually place those orders automatically. The database stays at the heart of everything.

---

## What stays the same

- **MC simulation engine** (`src/simulation.py`) — works well, no changes
- **Attendance adjustment logic** (`src/attendance.py`) — schedule conflict layering is solid
- **Portfolio analysis** (`src/portfolio.py`) — P&L simulation and Kelly sizing are fine
- **Backtest pipeline** (`scripts/run_backtest.py`, `scripts/backtest_pnl.py`) — don't touch
- **SQLite database as the source of truth** — no schema changes to existing tables

---

## Phase 1: Attendance Override System + Config in DB

**Goal:** Let you manually set or nudge a player's attendance probability before predictions are saved. This is the highest-priority change — it directly addresses the Magnus use case.

### 1.1 New DB table: `attendance_overrides`

```sql
CREATE TABLE attendance_overrides (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL,
    tourn_date    TEXT,           -- NULL = applies to next/all future predictions
    p_override    REAL    NOT NULL,  -- 0.0–1.0; replaces model estimate
    reason        TEXT,           -- e.g. "Magnus confirmed at Superbet, ~50% anyway"
    source        TEXT,           -- "manual", "tweet", "official announcement", etc.
    created_at    TEXT    DEFAULT (datetime('now')),
    expires_after TEXT            -- tourn_date after which this row is ignored
);
```

Usage example: insert a row for `MagnusCarlsen`, `tourn_date='2026-08-05'`, `p_override=0.45`, `reason="Likely resting post-Superbet but history suggests he shows up opportunistically"`.

### 1.2 Modify `data.py::load_and_prepare()`

After computing `p_participate` (the EWMA + isotonic floor + schedule adjustments), add a final step that checks `attendance_overrides` for any active rows and patches the series:

```python
# At the end of load_and_prepare():
overrides = _load_active_overrides(conn, as_of=target_date)
for username, p_override in overrides.items():
    if username in p_participate.index:
        p_participate[username] = p_override
```

Log a warning for each override applied so you can see them in the console when running predictions.

### 1.3 New DB table: `scheduling_config`

Move the hardcoded `CUT_PLAYERS`, `KEEP_PLAYERS`, and scheduling-conflict lists out of `make_predictions.py` into the DB:

```sql
CREATE TABLE scheduling_config (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL,
    config_type TEXT    NOT NULL,  -- 'cut', 'keep', 'conflict'
    start_date  TEXT,              -- NULL = always active
    end_date    TEXT,              -- NULL = no expiry
    reason      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
```

`make_predictions.py` reads active rows (where `end_date IS NULL OR end_date >= today`) instead of using hardcoded lists. This means you can add a scheduling conflict from a notebook cell rather than editing a script.

### 1.4 Migration script: `scripts/migrate_overrides.py`

One-time script that:

1. Creates the two new tables
2. Reads current hardcoded `CUT_PLAYERS` / `KEEP_PLAYERS` from `make_predictions.py` and seeds `scheduling_config` with them
3. Prints a summary of what was migrated

---

## Phase 2: Command Center Notebook

**Goal:** One notebook to rule Tuesday morning. Open it, run all cells top to bottom, see the model's predictions with your overrides, and know what to trade.

### File: `notebooks/command-center.ipynb`

Sections:

**Section 1 — Data freshness check**

- Query DB for latest `titled_tuesday_tournaments.date` and compare to today
- Warn if latest tournament data is more than 8 days old
- Show last 3 imported tournaments as a sanity check

**Section 2 — Run predictions**

- Call `make_predictions.py` as a subprocess (or import and call its main function directly if refactored to support that)
- Show the prediction timestamp and player count

**Section 3 — Attendance override editor**

- Load `attendance_overrides` from DB and display current active overrides as a DataFrame
- A cell below with a helper function:
  ```python
  set_override(username="MagnusCarlsen", p=0.45, reason="Post-Superbet rest, but opportunistic")
  ```
  that inserts/upserts the DB row and re-runs predictions
- Another cell to clear overrides for a given player

**Section 4 — Top predictions table**

- Load `latest_model_predictions` from DB
- Display sorted by `P_top3` (or a configurable threshold)
- Highlight rows where an override was applied (join against `attendance_overrides`)
- Show `p_participate`, `P_top1_given_play`, `P_top3`, `P_top5`, `P_top10` in a clean table

**Section 5 — Portfolio analysis**

- Load Kalshi portfolio from DB
- Run `run_portfolio_mc_score()` and `pnl_summary()`
- Show expected P&L distribution with quartiles

**Section 6 — Best new trades**

- Compare model probabilities against Kalshi market prices (requires fetching live prices — see Phase 4)
- Until the Kalshi API is wired up, accept a manually-updated `current_prices.csv` as input
- Show edge = `(model_prob - market_yes_ask) / market_yes_ask` for each position
- Rank by edge × open_interest as a proxy for Kelly-optimal bet size
- Show top 10 YES and top 10 NO opportunities

---

## Phase 3: Player Performance Notebook

**Goal:** A reference notebook for researching individual players before manual overrides.

### File: `notebooks/player-performance.ipynb`

Sections:

**Section 1 — Player lookup**

- Input cell: `PLAYER = "MagnusCarlsen"`
- Show: username, aliases, title, rating history from `standings`

**Section 2 — Attendance history**

- Bar chart: attended / did-not-attend per month, last 52 weeks
- Rolling 8-week attendance rate overlaid
- Mark dates where you have a scheduling conflict logged in DB

**Section 3 — Performance when present**

- Percentile rank distribution histogram (last 2 years)
- Score and tiebreak trends
- "Best finishes" table

**Section 4 — Model vs. reality**

- Load `historical_predictions` and `titled_tuesday_standings`
- For this player, show: model's `P_top3` prediction vs. actual finish each week
- Calibration plot: how accurate are the model's probability estimates?

**Section 5 — Peer comparison**

- Input: `PEERS = ["Hikaru", "Firouzja2003", "lachesisq"]`
- Side-by-side attendance rate and top-3 finish rate comparison

---

## Phase 4: Kalshi API Module

**Goal:** Lay the groundwork for automated order placement. Don't build the full pipeline yet — just a clean, well-tested client that the command center notebook can call.

### File: `src/kalshi_api.py`

**Authentication:**

```python
class KalshiClient:
    def __init__(self, api_key_id: str, private_key_path: str, env: str = "prod")
    def _sign_request(self, method, path, body=None) -> dict  # RSA signature headers
    def _get(self, path) -> dict
    def _post(self, path, body) -> dict
```

Kalshi uses RSA-signed JWT for authentication. Store `KALSHI_API_KEY_ID` and path to the private key PEM in environment variables (not in config.py), loaded via `python-dotenv`.

**Market data:**

```python
def get_market(self, ticker: str) -> dict         # single market price + metadata
def get_markets_by_event(self, event_ticker: str) -> list[dict]
def get_titled_tuesday_markets(self, date: str) -> list[dict]  # convenience wrapper
```

**Portfolio:**

```python
def get_positions(self) -> pd.DataFrame           # all current positions
def get_fills(self, ticker: str) -> list[dict]    # fill history for a market
```

**Order placement (with guard rails):**

```python
def place_order(
    self,
    ticker: str,
    side: str,         # "yes" | "no"
    count: int,        # number of contracts
    price: int,        # in cents (1–99)
    dry_run: bool = True  # default True — prints what it WOULD do, doesn't send
) -> dict
```

The `dry_run=True` default is critical — you should have to explicitly pass `dry_run=False` to actually place an order.

**Config additions to `src/config.py`:**

```python
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_ENV = os.environ.get("KALSHI_ENV", "prod")  # "demo" | "prod"
```

Add a `.env.example` file to the repo root documenting the required variables.

---

## Phase 5: Live Price Feed + Auto-Trade Script

**Goal:** Close the loop between model predictions and order placement.

### File: `scripts/fetch_kalshi_prices.py`

- Uses `KalshiClient` to pull current market prices for all active Titled Tuesday markets
- Writes results to a `kalshi_live_prices` DB table (with timestamp)
- Designed to run on demand or on a schedule (e.g., every 15 minutes on Tuesday)

### File: `scripts/recommend_trades.py`

- Loads `latest_model_predictions` + `kalshi_live_prices`
- Computes edge for each market
- Prints ranked list of recommended trades with suggested sizes (half-Kelly)
- Accepts `--execute` flag that calls `KalshiClient.place_order(dry_run=False)` after confirmation prompt

---

## Phase 6: Dashboard (eventual)

**Goal:** A live-updating view of the model's top trade recommendations, visible without opening Jupyter.

**Technology choice — Streamlit** (simplest to build, runs locally):

- `dashboard/app.py` — single-file Streamlit app
- Reads directly from `titled_tuesday.db`
- Pages: (1) Top trades, (2) Player attendance table with override controls, (3) Portfolio P&L, (4) Backtest results

Run with: `streamlit run dashboard/app.py`

No deployment needed initially — local only. If you later want it accessible remotely, Streamlit Cloud can host it from the repo.

---

## Implementation Order

| Phase | What                                                                     | Effort | Priority      |
| ----- | ------------------------------------------------------------------------ | ------ | ------------- |
| 1     | Attendance override table + load_and_prepare() patch + scheduling_config | ~3h    | **This week** |
| 2     | Command center notebook                                                  | ~2h    | **This week** |
| 3     | Player performance notebook                                              | ~2h    | Next          |
| 4     | `src/kalshi_api.py` (auth + market data + order stub)                    | ~3h    | Next          |
| 5     | Live price fetch + recommend_trades script                               | ~2h    | After 4       |
| 6     | Streamlit dashboard                                                      | ~4h    | Eventually    |

---

## Files changed / created (summary)

**New files:**

- `scripts/migrate_overrides.py` — DB migration (new tables + seed from hardcoded lists)
- `notebooks/command-center.ipynb` — primary weekly workflow
- `notebooks/player-performance.ipynb` — player research tool
- `src/kalshi_api.py` — Kalshi REST client
- `scripts/fetch_kalshi_prices.py` — pull live market prices into DB
- `scripts/recommend_trades.py` — ranked trade recommendations with optional execution
- `dashboard/app.py` — Streamlit dashboard (Phase 6)
- `.env.example` — documents required environment variables

**Modified files:**

- `src/data.py` — add `_load_active_overrides()` + apply at end of `load_and_prepare()`
- `src/config.py` — add Kalshi API env var refs
- `scripts/make_predictions.py` — read `scheduling_config` from DB instead of hardcoded lists

**Unchanged:**

- `src/simulation.py`, `src/attendance.py`, `src/portfolio.py`
- `scripts/run_backtest.py`, `scripts/backtest_pnl.py`
- All existing notebooks (kept as-is; command-center replaces the ad-hoc workflow)
