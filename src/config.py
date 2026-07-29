"""Central configuration for all Titled Tuesday analysis modules."""
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH      = PROJECT_ROOT / 'data' / 'titled_tuesday.db'
MODELS_DIR   = PROJECT_ROOT / 'models'
DATA_DIR     = PROJECT_ROOT / 'data'
BACKTEST_DIR = PROJECT_ROOT / 'data' / 'backtest'

# ── Monte Carlo hyperparameters ───────────────────────────────────────────────
DATA_CUTOFF          = '2020-01-01'   # earliest date used for rank-pct history
SKILL_DECAY          = 0.96
PARTICIPATION_DECAY  = 0.85
MIN_PARTICIPATION_RATE = 0.005
N_SIMS               = 100_000
N_VALUES             = [1, 3, 5, 8, 10]
MIN_P                = 0.0
MIN_APPEARANCES      = 0
CHUNK                = 10_000
SEED                 = 42
_SCORE_COMPOSITE_SCALE = 10_000.0    # score*scale + tiebreak -> single sortable float

# ── Attendance model ──────────────────────────────────────────────────────────
DATA_START = '2025-09-02'   # single-session era start; pre-era data excluded from ML training
MIN_APP    = 3              # minimum weeks attended to appear in training set

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
