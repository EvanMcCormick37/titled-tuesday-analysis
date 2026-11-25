# Model and data layer (`src/`)

The market maker (`src/mm/`) and the attendance researcher (`src/research/`) have their own `CLAUDE.md`. The trade queue modules (`trade_plan`, `trading`, `strategies`, `job_queue`, `job_runner`) are described in the root `CLAUDE.md`, because the API and the worker both depend on their rules.

## Key modules

- **`config.py`**: MC hyperparameters (`N_SIMS`, `SKILL_DECAY`, `PARTICIPATION_DECAY`), local DB path, attendance cutoffs. **No `TOURN_DATE` constant**: use `pipeline.next_tourn_date(spec)` (one week after the latest date in the market's standings table).
- **`db.py`**: dual backend chosen at import from `$DATABASE_URL` (`postgres(ql)://` → psycopg3, else SQLite at `config.DB_PATH`). `get_conn()` translates `?` → `%s` on Postgres; `get_engine()` for pandas. Write predictions with `replace_market_rows`.
- **`data.py`**: `load_and_prepare()`: decay-weighted skill history and EWMA participation, with `attendance_conflicts` weeks removed from the denominator, so `p_participate` is a conflict-free attendance rate. Takes `as_of` to rebuild a past week from earlier history only (leave unset in production).
- **`simulation.py`**: MC kernels. Production uses `build_player_pool_score()` (composite `score * 10000 + tiebreak`) with `run_simulation_ranked`; the perf pool is for backtests.
- **`pipeline.py`**: `run_raw`, `run_adjusted`, `load_adjustments`, `next_tourn_date`. `run_adjusted(save_official=True)` also archives to `historical_predictions`. Adjustment priority: `p_nudges` (log-odds shifts, after `global_nudge`) → `cut_players` → `p_participate_overrides` → `keep_players`; last write wins per (player, type).
- **`attendance.py`**: attendance helpers, including `_compute_conflict_rates` (a player's historical attendance on conflict weeks). **`attendance_model.py`**: the optional two-part attendance model (below).
- **`markets/`**: one `MarketSpec` per weekly event (see "Multiple markets" in the root `CLAUDE.md`).
- **`kalshi_markets.py`** / **`kalshi_tt.py`**: Kalshi helpers per spec; positions join the kalshi-core `fills` table for fee-inclusive cost. Import `KalshiClient` from `kalshi_core`, not from here.
- **`portfolio.py`**: portfolio MC and Kelly sizing. tt-worker runs `scripts/portfolio_pnl.py` as a subprocess for the Portfolio page's `pnl` snapshot, only when positions or predictions change.
- **`backtest_runs.py`**: backtest scoring; **`vip_players.py`**: the fixed cohort of traded players used to judge models.
- **`bullet_brawl_ingest.py`**: Bullet Brawl standings ingestion and username capitalization (see `scripts/scraping/CLAUDE.md`).

## Model settings (`ModelConfig` in `markets/base.py`)

- `score_noise_sigma` (score points) sets a market's Gaussian smoothing in `run_raw` and `run_adjusted`. Titled Tuesday's is 0.01 (100 composite units, `SCORE_GAUSSIAN_KERNEL_SIGMA_NARROW`), which randomizes tiebreaks without moving anyone across a score bucket; Bullet Brawl's is 10, the winner of its sigma backtest; Freestyle Friday uses Titled Tuesday's 0.01. **Never set it to 0**: replayed results would keep their exact historical tiebreaks, so ties resolve deterministically. The `ModelConfig` default is 0.01 for that reason.
- `played_filter` is the SQL condition for a row that counts as attending; Bullet Brawl uses `games > 0` (registered but never paired is not attending).
- Optional, off by default (Freestyle Friday uses the first and third):
  - `attendance_model='two_part'` (`attendance_model.py`): a walk-forward logistic model of the choice to play, fitted on conflict-free weeks from a fast and a slow attendance rate, weeks since last played, appearances and past conflict rate.
  - `conflict_deduction=False` keeps past conflict weeks in the attendance denominator.
  - `skill_prior_events=k` mixes the field's recent results into each player's skill history with weight k / (k + effective events), so players with little or old history are pulled toward the field.
- `scripts/run_backtest.py` compares these as `narrow_*` variants (`BacktestProfile.overrides`); with `--market bullet_brawl` it runs the sigma comparison. Add a market's backtest to `PROFILES` in that script.

## Player names

Chess.com usernames (in standings) map to display names via `player_information.player_name`; Kalshi contracts are labelled with display names. `is_default=1` marks the most-recently-active Titled Tuesday username per player.

Players sometimes use a different account per event, so trading and `run_adjusted` use `data.market_player_to_username(usernames, spec)`: among a player's accounts in the market's predictions, the one seen most recently in that market wins. `data.match_player` looks a Kalshi name up exactly, then falls back to an unambiguous word-set match (e.g. "Renato Terry" for "Renato Terry Celis"). Names that still don't match show as unmatched on the Edges page; add them to `_PLAYER_ALIASES` in `data.py`.
