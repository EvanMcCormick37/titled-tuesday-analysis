"""
Attendance model: predict P(player shows up to next Titled Tuesday).

Trains 3 classifiers on post-DATA_START tournament history:
  logistic       – L2 logistic regression (interpretable baseline)
  random_forest  – Random Forest
  gradient_boost – Gradient Boosted Trees

Models are saved to models/attendance_{name}.pkl and loaded via load_models().
The get_p_participate_for_mc() function is the drop-in replacement for the
EWMA-based p_participate used in src/data.py.
"""

import pickle
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .config import DB_PATH, MODELS_DIR, DATA_START, MIN_APP, FEATURE_COLS, PARTICIPATION_DECAY

warnings.filterwarnings('ignore')


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    """Load post-era standings from DB, compute rank_pct and rating_num."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"SELECT * FROM titled_tuesday_standings WHERE date >= '{DATA_START}'",
        conn, parse_dates=['date'],
    )
    conn.close()

    df = (
        df.sort_values('rank')
          .drop_duplicates(subset=['tournament_slug', 'username'], keep='first')
          .reset_index(drop=True)
    )
    n_in_event = df.groupby('tournament_slug')['username'].transform('count')
    df['rank_pct']   = 1.0 - (df['rank'].astype(float) - 1) / (n_in_event - 1)
    df['rating_num'] = pd.to_numeric(df['rating'], errors='coerce').fillna(2500.0)
    return df


def get_weekly_slots(df: pd.DataFrame) -> pd.DataFrame:
    """One row per unique tournament date (week), sorted chronologically."""
    return (
        df.groupby('date').size()
          .reset_index(name='n_players')
          .sort_values('date')
          .reset_index(drop=True)
    )


# ── Feature engineering ───────────────────────────────────────────────────────

def _compute_features(
    tdate: np.datetime64,
    pd_uniq: np.ndarray,
    rp_: np.ndarray,
    ra_: np.ndarray,
    week_dates_sorted: np.ndarray,
    start_ts: np.datetime64,
) -> dict:
    n = len(pd_uniq)

    ts   = pd.Timestamp(tdate)
    mo_s = np.sin(2 * np.pi * ts.month / 12)
    mo_c = np.cos(2 * np.pi * ts.month / 12)
    wk   = ts.isocalendar().week
    wk_s = np.sin(2 * np.pi * wk / 52)
    wk_c = np.cos(2 * np.pi * wk / 52)

    days_since = int((tdate - pd_uniq[-1]) / np.timedelta64(1, 'D'))
    i_td = int(np.searchsorted(week_dates_sorted, tdate, side='left'))

    def att_rate(n_weeks: int) -> float:
        cut      = tdate - np.timedelta64(int(n_weeks * 7), 'D')
        n_avail  = i_td - int(np.searchsorted(week_dates_sorted, cut, side='left'))
        n_played = n - int(np.searchsorted(pd_uniq, cut, side='left'))
        return n_played / max(n_avail, 1)

    total_wk = max(int((tdate - start_ts) / np.timedelta64(7, 'D')), 1)
    Z        = (1.0 - PARTICIPATION_DECAY ** total_wk) / (1.0 - PARTICIPATION_DECAY)
    wdiffs   = ((tdate - pd_uniq) / np.timedelta64(7, 'D')).astype(float)
    decay    = float(np.sum(PARTICIPATION_DECAY ** wdiffs) / Z)

    n_all = len(rp_)
    rec_n = min(8, n_all)
    rat   = float(ra_[-1])
    trend = float(np.mean(ra_[-4:]) - np.mean(ra_[-8:-4])) if n_all >= 8 else 0.0

    return {
        'month_sin':            mo_s,
        'month_cos':            mo_c,
        'week_sin':             wk_s,
        'week_cos':             wk_c,
        'log_weeks_since_last': np.log1p(days_since / 7.0),
        'att_rate_4w':          att_rate(4),
        'att_rate_8w':          att_rate(8),
        'att_rate_16w':         att_rate(16),
        'decay_score':          decay,
        'log_n_appearances':    np.log1p(n),
        'avg_rank_pct_recent':  float(np.mean(rp_[-rec_n:])),
        'avg_rank_pct_all':     float(np.mean(rp_)),
        'current_rating':       rat,
        'rating_trend':         trend,
    }


# ── Dataset builder ───────────────────────────────────────────────────────────

def build_dataset(df: pd.DataFrame, weekly_slots: pd.DataFrame) -> pd.DataFrame:
    """Build training rows: one per (player, tournament-week) in the player's active window."""
    start_ts       = np.datetime64(DATA_START, 'D').astype('datetime64[ns]')
    week_dates     = weekly_slots['date'].values.astype('datetime64[ns]')
    week_dates_srt = np.sort(week_dates)
    n_users        = df['username'].nunique()
    rows = []

    for u_idx, (username, hist) in enumerate(df.groupby('username')):
        if (u_idx + 1) % 500 == 0:
            print(f'    {u_idx + 1:,}/{n_users:,} players processed...')

        hist = hist.sort_values('date').reset_index(drop=True)
        attended_dates  = set(hist['date'].values.astype('datetime64[ns]'))
        n_unique_weeks  = len(attended_dates)
        if n_unique_weeks < MIN_APP:
            continue

        first_dt = hist['date'].iloc[0].to_datetime64()
        last_dt  = hist['date'].iloc[-1].to_datetime64()
        wm       = (week_dates >= first_dt) & (week_dates <= last_dt)
        ps_dates = week_dates[wm]
        if len(ps_dates) == 0:
            continue

        h_dates   = hist['date'].values.astype('datetime64[ns]')
        h_rp      = hist['rank_pct'].values.astype(float)
        h_ratings = hist['rating_num'].values.astype(float)
        ptr = 0

        for tdate in ps_dates:
            while ptr < len(h_dates) and h_dates[ptr] < tdate:
                ptr += 1
            if ptr == 0:
                continue
            pd_uniq = np.unique(h_dates[:ptr])
            feats   = _compute_features(
                tdate, pd_uniq, h_rp[:ptr], h_ratings[:ptr],
                week_dates_srt, start_ts,
            )
            feats['username'] = username
            feats['date']     = pd.Timestamp(tdate)
            feats['label']    = int(tdate in attended_dates)
            rows.append(feats)

    return pd.DataFrame(rows)


# ── Models ────────────────────────────────────────────────────────────────────

def make_models() -> dict:
    return {
        'logistic': Pipeline([
            ('scaler', StandardScaler()),
            ('clf', LogisticRegression(
                C=1.0, max_iter=1000, class_weight='balanced', random_state=42,
            )),
        ]),
        'random_forest': RandomForestClassifier(
            n_estimators=200, max_depth=8, min_samples_leaf=20,
            class_weight='balanced', random_state=42, n_jobs=-1,
        ),
        'gradient_boost': GradientBoostingClassifier(
            n_estimators=150, max_depth=3, learning_rate=0.05,
            min_samples_leaf=20, subsample=0.8, random_state=42,
        ),
    }


# ── Cross-validation ──────────────────────────────────────────────────────────

def cross_validate(dataset: pd.DataFrame, models: dict) -> dict:
    data = dataset.dropna(subset=FEATURE_COLS + ['label']).sort_values('date')
    print(f'  (CV on {len(data):,} rows, TimeSeriesSplit n_splits=4)')
    X = data[FEATURE_COLS].values
    y = data['label'].values

    tscv       = TimeSeriesSplit(n_splits=4)
    cv_results = {}
    for name, model in models.items():
        aucs, briers, lls = [], [], []
        for train_idx, val_idx in tscv.split(X):
            model.fit(X[train_idx], y[train_idx])
            proba = model.predict_proba(X[val_idx])[:, 1]
            aucs.append(roc_auc_score(y[val_idx], proba))
            briers.append(brier_score_loss(y[val_idx], proba))
            lls.append(log_loss(y[val_idx], proba))
        cv_results[name] = {
            'auc':      round(float(np.mean(aucs)),   4),
            'brier':    round(float(np.mean(briers)), 4),
            'log_loss': round(float(np.mean(lls)),    4),
        }
        print(f'  {name:<20}  AUC={cv_results[name]["auc"]:.3f}  '
              f'Brier={cv_results[name]["brier"]:.3f}  '
              f'LogLoss={cv_results[name]["log_loss"]:.3f}')
    return cv_results


def train_final(dataset: pd.DataFrame, models: dict) -> dict:
    data = dataset.dropna(subset=FEATURE_COLS + ['label']).sort_values('date')
    X, y = data[FEATURE_COLS].values, data['label'].values
    final = {}
    for name, model in models.items():
        model.fit(X, y)
        final[name] = model
        print(f'  {name} - trained on {len(y):,} rows')
    return final


# ── Persistence ───────────────────────────────────────────────────────────────

def save_models(final_models: dict, cv_results: dict) -> None:
    MODELS_DIR.mkdir(exist_ok=True)
    for name, model in final_models.items():
        path = MODELS_DIR / f'attendance_{name}.pkl'
        with open(path, 'wb') as f:
            pickle.dump({'model': model, 'features': FEATURE_COLS, 'cv': cv_results.get(name, {})}, f)
        print(f'  Saved -> {path}')


def load_models() -> dict:
    models = {}
    for path in sorted(MODELS_DIR.glob('attendance_*.pkl')):
        name = path.stem.replace('attendance_', '')
        with open(path, 'rb') as f:
            models[name] = pickle.load(f)
        print(f'  Loaded {path}  (CV: {models[name].get("cv", {})})')
    return models


# ── Prediction ────────────────────────────────────────────────────────────────

def build_prediction_features(
    df: pd.DataFrame,
    weekly_slots: pd.DataFrame,
    target_date: pd.Timestamp,
) -> pd.DataFrame:
    start_ts       = np.datetime64(DATA_START, 'D').astype('datetime64[ns]')
    tdate          = target_date.to_datetime64().astype('datetime64[ns]')
    week_dates_srt = np.sort(weekly_slots['date'].values.astype('datetime64[ns]'))

    rows = []
    for username, hist in df.groupby('username'):
        hist    = hist.sort_values('date').reset_index(drop=True)
        h_dates   = hist['date'].values.astype('datetime64[ns]')
        h_rp      = hist['rank_pct'].values.astype(float)
        h_ratings = hist['rating_num'].values.astype(float)

        mask    = h_dates < tdate
        pd_uniq = np.unique(h_dates[mask])
        if len(pd_uniq) < MIN_APP:
            continue
        feats = _compute_features(
            tdate, pd_uniq, h_rp[mask], h_ratings[mask],
            week_dates_srt, start_ts,
        )
        feats['username'] = username
        feats['date']     = target_date
        rows.append(feats)

    return pd.DataFrame(rows)


def predict_attendance(
    df: pd.DataFrame,
    weekly_slots: pd.DataFrame,
    models_payload: dict,
    target_date: pd.Timestamp,
) -> pd.DataFrame:
    feat_df = build_prediction_features(df, weekly_slots, target_date)
    if feat_df.empty:
        print('No eligible players found.')
        return pd.DataFrame()

    X   = feat_df[FEATURE_COLS].values
    out = feat_df[['username', 'date']].copy()
    for name, payload in models_payload.items():
        out[f'p_{name}'] = payload['model'].predict_proba(X)[:, 1]

    prob_cols      = [c for c in out.columns if c.startswith('p_')]
    out['p_ensemble'] = out[prob_cols].mean(axis=1)
    return out.sort_values('p_ensemble', ascending=False).reset_index(drop=True)


def get_p_participate_for_mc(
    df: pd.DataFrame,
    weekly_slots: pd.DataFrame,
    models_payload: dict,
    target_date: pd.Timestamp,
    model_name: str = 'gradient_boost',
) -> pd.Series:
    """
    Drop-in replacement for the EWMA p_participate used in src/data.load_and_prepare().
    Returns a Series indexed by username. Empty if no eligible players exist.
    """
    preds = predict_attendance(df, weekly_slots, models_payload, target_date)
    if preds.empty:
        return pd.Series(dtype=float, name='p_participate')
    col = f'p_{model_name}' if f'p_{model_name}' in preds.columns else 'p_ensemble'
    return preds.set_index('username')[col].rename('p_participate')


# ── Feature inspection ────────────────────────────────────────────────────────

def print_feature_importances(final_models: dict) -> None:
    print('\n-- Logistic Regression Coefficients (by |coef|) -----------------')
    pipe  = final_models['logistic']
    coefs = pipe.named_steps['clf'].coef_[0]
    for feat, coef in sorted(zip(FEATURE_COLS, coefs), key=lambda x: abs(x[1]), reverse=True):
        bar  = '#' * int(abs(coef) * 20)
        sign = '+' if coef > 0 else '-'
        print(f'  {feat:<28} {sign}{abs(coef):.4f}  {bar}')

    print('\n-- Random Forest Feature Importances ----------------------------')
    rf = final_models['random_forest']
    for feat, imp in sorted(zip(FEATURE_COLS, rf.feature_importances_), key=lambda x: x[1], reverse=True):
        print(f'  {feat:<28} {imp:.4f}  {"#" * int(imp * 100)}')

    print('\n-- Gradient Boost Feature Importances ---------------------------')
    gb = final_models['gradient_boost']
    for feat, imp in sorted(zip(FEATURE_COLS, gb.feature_importances_), key=lambda x: x[1], reverse=True):
        print(f'  {feat:<28} {imp:.4f}  {"#" * int(imp * 100)}')
