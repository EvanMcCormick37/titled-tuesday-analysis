# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

# Walk-forward backtest (writes to backtest_predictions table)
python scripts/run_backtest.py

# Simulate historical P&L from backtest predictions vs Kalshi market closes
python scripts/backtest_pnl.py
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
                                       Kalshi API
```

### Key Modules

**`src/config.py`** — Single source of truth for all constants: DB path, MC hyperparameters (`N_SIMS=100_000`, `SKILL_DECAY=0.96`, `PARTICIPATION_DECAY=0.85`), attendance model cutoffs. Change values here, not inline.

**`src/data.py`** — `load_and_prepare()` reads standings from SQLite, computes decay-weighted skill history and EWMA participation rates, and applies an isotonic regression floor (`models/isotonic_regression.joblib`) to smooth low-attendance players.

**`src/simulation.py`** — Two Monte Carlo modes:
- `run_simulation()` + `build_player_pool()`: rank-percentile based (legacy)
- `run_simulation_score()` + `build_player_pool_score()`: composite score (score×10000 + tiebreak), the current default

Runs in NumPy-vectorized chunks (`CHUNK=10_000` sims per batch). Returns `P_top{1,3,5,8,10}_given_play` (conditional on attendance) and `p_participate`.

**`src/pipeline.py`** — `run_predictions()` chains the full pipeline: load → build pool → raw simulation → apply in-memory adjustments (nudges/cuts/overrides/keeps) → adjusted simulation → optionally save to DB. The adjustment priority order is: `p_nudges` → `cut_players` → `p_participate_overrides` → `keep_players`.

**`src/kalshi_api.py`** — REST client for Kalshi. Uses RSA-PSS (SHA-256) signatures. Loads key from `.env` (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV`). Supports `prod` and `demo` environments.

**`src/trading.py`** — Two strategies:
- `place_bids()`: computes fair price = `p_participate × P_top{N}_given_play`, bids at `max(fair/markup, fair - max_discount)`, cancels stale orders if new bid is lower, pulls back if market ask is at or below the computed bid
- `take_trades()`: fetches live asks concurrently, filters by ROI threshold and minimum quantity (to avoid iceberg decoys), fires fill-or-kill orders via `ThreadPoolExecutor`

**`src/portfolio.py`** — Reads `kalshi_portfolio` table (populated from Kalshi API snapshots), runs portfolio-level MC for P&L simulation and Kelly sizing.

### Database Schema (SQLite, `data/titled_tuesday.db`)

Core tables:
- `titled_tuesday_standings` — per-player results per tournament (rank, score, tiebreak, etc.)
- `titled_tuesday_tournaments` — tournament metadata (date, session, num_players, winner)
- `player_information` — Chess.com enrichment; `is_default=1` marks the canonical username per player
- `latest_model_predictions` — current adjusted predictions (overwritten each run)
- `latest_model_predictions_raw` — pre-adjustment predictions (for comparison)
- `historical_predictions` — archived past predictions
- `backtest_predictions` — walk-forward backtest output
- `kalshi_market_snapshots` — historical Kalshi ask/bid closes for backtesting P&L
- `other_events` / `other_event_participants` — concurrent OTB tournaments (used for attendance modeling)

### Username / Player Name Mapping

Chess.com usernames (in standings) are mapped to display names via `player_information.player_name`. Kalshi market tickers use these display names. The `is_default` flag in `player_information` marks the most-recently-active username per player when one player has multiple accounts.

### Kalshi Market Tickers

Markets follow the pattern `TTUES-{date}-{EVENT}-{PLAYER}` where `EVENT` encodes the top-N threshold (e.g., `TOP1`, `TOP3`). The ticker parsing logic in `trading.py` and `backtest_pnl.py` extracts N and player name from these tickers.

### API Cache

Chess.com API responses are cached in `data/api_cache/` (JSON files keyed by username) with thread-safe access and exponential backoff on rate limits. Cached data is reused unless explicitly refreshed.

## Environment Setup

Requires a `.env` file at the project root:
```
KALSHI_API_KEY_ID=<uuid>
KALSHI_PRIVATE_KEY_PATH=trading-key.txt
KALSHI_ENV=prod   # or demo
```

`trading-key.txt` holds the RSA private key for API signing. Both files are gitignored.
