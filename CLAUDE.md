# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@../kalshi-core/CLAUDE.md

## Project Purpose

Production Python trading system for betting on chess.com's weekly **Titled Tuesday** blitz tournament using Kalshi prediction markets. The system:
1. Scrapes and stores historical TT results from Chess.com
2. Predicts each player's probability of attending and finishing in the top N
3. Places and executes orders on Kalshi using those fair-value estimates

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Scrape latest Titled Tuesday results into DB
python scripts/scraping/update_titled_tuesday.py

# Run predictions for the upcoming tournament
python scripts/make_predictions.py

# Place resting bids on Kalshi (dry run by default, pass --execute to go live)
python scripts/place_bids.py

# Fire taker orders for high-ROI opportunities
python scripts/take_trades.py

# Walk-forward joint backtest of score vs perf-rating MC (writes to `backtest` table)
python scripts/run_backtest.py
```

The `notebooks/command-center.ipynb` notebook is the primary interactive control surface for adjusting predictions before each tournament (nudges, cuts, overrides).

## Architecture Overview

### Data Flow

```
Chess.com → scripts/scraping/update_titled_tuesday.py → SQLite DB (data/titled_tuesday.db)
                                                              ↓
                                                    src/data.py (load_and_prepare)
                                                              ↓
                                                    src/simulation.py (100k Monte Carlo)
                                                              ↓
                                              latest_model_predictions table
                                             /                              \
                                src/trading.py                        notebooks/
                            (place_bids / take_trades)             (analysis, backtest)
                                            ↓
                                 kalshi_core.KalshiClient
```

### Key Modules

**`src/config.py`** — Single source of truth for all constants: DB path, MC hyperparameters (`N_SIMS=100_000`, `SKILL_DECAY=0.96`, `PARTICIPATION_DECAY=0.85`), attendance model cutoffs. Change values here, not inline.

**`src/data.py`** — `load_and_prepare()` reads standings from SQLite, computes decay-weighted skill history and EWMA participation rates, and applies an isotonic regression floor (`models/isotonic_regression.joblib`) to smooth low-attendance players.

**`src/simulation.py`** — Monte Carlo kernels + three player-pool builders (one per skill signal):
- `build_player_pool()` + `run_simulation()`: rank-percentile (legacy, float32)
- `build_player_pool_score()`: composite `score * 10000 + tiebreak` (production default)
- `build_player_pool_perf()`: `performance_rating` (aroc-based Elo estimate; used by backtest)

The score and perf pools feed a shared generic kernel `run_simulation_ranked` (also exposed as `run_simulation_score` for back-compat). Runs in NumPy-vectorized chunks (`CHUNK=10_000` sims per batch). Returns `P_top{1,3,5,8,10}_given_play` (conditional on attendance) and `p_participate`.

**`src/pipeline.py`** — `run_predictions()` chains the full pipeline: load → build pool → raw simulation → apply in-memory adjustments (nudges/cuts/overrides/keeps) → adjusted simulation → optionally save to DB. The adjustment priority order is: `p_nudges` → `cut_players` → `p_participate_overrides` → `keep_players`.

**`src/kalshi_tt.py`** — Titled Tuesday-specific Kalshi helpers built on top of `kalshi_core.KalshiClient`. Contains `_TT_EVENT_TEMPLATES`, `_next_tuesday()`, and three standalone functions: `get_tt_markets()`, `get_tt_asks()`, `get_tt_positions_df()`. Import `KalshiClient` from `kalshi_core`, not from here.

**`src/trading.py`** — Two strategies:
- `place_bids()`: computes fair price = `p_participate × P_top{N}_given_play`, bids at `max(fair/markup, fair - max_discount)`, cancels stale orders if new bid is lower, pulls back if market ask is at or below the computed bid
- `take_trades()`: fetches live asks via `get_tt_asks()`, filters by ROI threshold and minimum quantity (to avoid iceberg decoys), fires fill-or-kill orders via `ThreadPoolExecutor`

Bid math (`bid_cents`, `best_ask`, `apply_pullback`, `kalshi_order_price`) imported from `kalshi_core`.

**`src/portfolio.py`** — Runs portfolio-level MC for P&L simulation and Kelly sizing. `build_portfolio()` converts a positions DataFrame into numpy arrays for vectorized simulation. `pnl_summary`, `portfolio_kelly`, and `kalshi_order_price` are imported from `kalshi_core` and re-exported here for backward compatibility.

### Database Schema (SQLite, `data/titled_tuesday.db`)

Core tables:
- `titled_tuesday_standings` — per-player results per tournament (rank, score, tiebreak, etc.)
- `titled_tuesday_tournaments` — tournament metadata (date, session, num_players, winner)
- `player_information` — Chess.com enrichment; `is_default=1` marks the canonical username per player
- `latest_model_predictions` — current adjusted predictions (overwritten each run)
- `latest_model_predictions_raw` — pre-adjustment predictions (for comparison)
- `historical_predictions` — archived past predictions
- `backtest` — joint walk-forward backtest output; row = (tourn_date, model, username) with `model` in {`score`, `perf`}, includes `played` and `actual_rank` for calibration analysis
- `kalshi_market_snapshots` — historical Kalshi ask/bid closes for backtesting P&L
- `other_events` / `other_event_participants` — concurrent OTB tournaments (used for attendance modeling)

### Username / Player Name Mapping

Chess.com usernames (in standings) are mapped to display names via `player_information.player_name`. Kalshi market tickers use these display names. The `is_default` flag in `player_information` marks the most-recently-active username per player when one player has multiple accounts.

### Kalshi Market Tickers

TT markets use two event ticker patterns, defined in `src/kalshi_tt.py`:
- `KXTITLEDTUESDAY-{date}-{PLAYER}` — winner market (N=1)
- `KXTITLEDTUESTOP-{date}T{N}-{PLAYER}` — top-N markets (N=3, 5, 8)

`{date}` is in Kalshi's `YYMONDD` format (e.g. `26SEP16`), produced by `_to_kalshi_date()` from `kalshi_core`. The ticker parsing logic in `trading.py` and `backtest_pnl.py` extracts N and player name from these patterns.

### API Cache

Chess.com API responses are cached in `data/api_cache/` (JSON files keyed by username) with thread-safe access and exponential backoff on rate limits. Cached data is reused unless explicitly refreshed.

## Environment Setup

Kalshi credentials live in `~/.kalshi/` (not in this repo):
```
~/.kalshi/
    .env              ← KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV
    trading-key.txt   ← RSA private key for API signing
```

`kalshi_core.KalshiClient` loads from this location automatically. No `.env` file is needed in the project directory.
