"""Central configuration for all Titled Tuesday analysis modules."""
import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH      = PROJECT_ROOT / 'data' / 'titled_tuesday.db'
MODELS_DIR   = PROJECT_ROOT / 'models'
DATA_DIR     = PROJECT_ROOT / 'data'
BACKTEST_DIR = PROJECT_ROOT / 'data' / 'backtest'

# ── Monte Carlo hyperparameters ───────────────────────────────────────────────
TOURN_DATE = "2026-09-29"   # Next Tuesday's date (YYYY-MM-DD)
DATA_CUTOFF          = '2020-01-01'   # earliest date used for rank-pct history
SKILL_DECAY          = 0.96
PARTICIPATION_DECAY  = 0.75
MIN_PARTICIPATION_RATE = 0.005
N_SIMS               = 100_000
N_VALUES             = [1, 3, 5, 8, 10]
MIN_P                = 0.0
MIN_APPEARANCES      = 5
CHUNK                = 10_000
SEED                 = 42
SCORE_COMPOSITE_SCALE = 10_000.0    # score*scale + tiebreak -> single sortable float
# Gaussian smoothing sigmas (composite-signal units — i.e. after multiplication by SCORE_COMPOSITE_SCALE).
SCORE_GAUSSIAN_KERNEL_SIGMA_WIDE   = 5000.0    # 0.5 score-points; smooths across whole score buckets
SCORE_GAUSSIAN_KERNEL_SIGMA_NARROW = 100.0     # ~2x tiebreak magnitude; smooths tiebreaks, keeps score ranks intact

# ── Kalshi API ────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_ENV              = os.environ.get("KALSHI_ENV", "prod")  # "demo" | "prod"

# ── Attendance model ──────────────────────────────────────────────────────────
DATA_START = '2025-09-02'   # single-session era start; pre-era data excluded from ML training
MIN_APP    = 3              # minimum weeks attended to appear in training set

# ── Season-shift discount ─────────────────────────────────────────────────────
# Tournaments before this date have their participation weight multiplied by
# SEASON_SHIFT_FACTOR, downweighting pre-shift history relative to the new CCT
# season era.  Set SEASON_SHIFT_DATE = None to disable.
SEASON_SHIFT_DATE   = '2026-08-30 00:00:00'
SEASON_SHIFT_FACTOR = 0.5

# FEATURE_COLS = [
#     'month_sin', 'month_cos',
#     'week_sin',  'week_cos',
#     'log_weeks_since_last',
#     'att_rate_4w', 'att_rate_8w', 'att_rate_16w',
#     'decay_score',
#     'log_n_appearances',
#     'avg_rank_pct_recent',
#     'avg_rank_pct_all',
#     'current_rating',
#     'rating_trend',
# ]
