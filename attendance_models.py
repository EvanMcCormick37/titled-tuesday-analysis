#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf-8-sig'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
"""
attendance_models.py - Train logistic regression models for P(player shows up)

Predicts weekly attendance: will a given player show up to next week's Titled
Tuesday? Trained exclusively on data from 2025-09-02 onwards, when Titled
Tuesday switched from a twice-weekly (early/late session) format to a single
weekly tournament. Pre-era data is excluded entirely to avoid contaminating
the models with stale session-preference and bi-weekly attendance patterns.

Trains 3 classifiers and saves them to models/:
  1. logistic       - L2 logistic regression (interpretable baseline)
  2. random_forest  - Random Forest
  3. gradient_boost - Gradient Boosted Trees

Usage
-----
  python attendance_models.py             # train and save
  python attendance_models.py --predict   # predict next week
  python attendance_models.py --predict --date 2026-07-15 --top 50
"""

import argparse
import pickle
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

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────

DATA_PATH  = Path("data/titled_tuesday_standings_modern.csv")
MODELS_DIR = Path("models")
DATA_START = "2025-09-02"  # single-session era begins; pre-era data excluded
PART_DECAY = 0.85          # must match bootstrap_mc_kalshi.py
MIN_APP    = 3             # minimum weeks attended to be included in training
FEATURE_COLS = [
    "month_sin", "month_cos",
    "week_sin",  "week_cos",
    "log_weeks_since_last",
    "att_rate_4w", "att_rate_8w", "att_rate_16w",
    "decay_score",
    "log_n_appearances",
    "avg_rank_pct_recent",
    "avg_rank_pct_all",
    "current_rating",
    "rating_trend",
]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH, parse_dates=["date"])
    df = df[df["date"] >= DATA_START].copy()
    df = (
        df.sort_values("rank")
          .drop_duplicates(subset=["tournament_slug", "username"], keep="first")
          .reset_index(drop=True)
    )
    n_in_event = df.groupby("tournament_slug")["username"].transform("count")
    df["rank_pct"] = 1.0 - (df["rank"].astype(float) - 1) / (n_in_event - 1)
    df["rating_num"] = pd.to_numeric(df["rating"], errors="coerce").fillna(2500.0)
    return df


def get_weekly_slots(df: pd.DataFrame) -> pd.DataFrame:
    """One row per unique tournament date (week), sorted chronologically."""
    return (
        df.groupby("date")
          .size().reset_index(name="n_players")
          .sort_values("date")
          .reset_index(drop=True)
    )


# ── Dataset builder ───────────────────────────────────────────────────────────

def _compute_features(
    tdate: np.datetime64,
    pd_uniq: np.ndarray,  # unique past attendance dates (sorted, datetime64[ns])
    rp_: np.ndarray,      # all past rank percentiles (for performance features)
    ra_: np.ndarray,      # all past ratings
    week_dates_sorted: np.ndarray,  # sorted unique tournament-week dates
    start_ts: np.datetime64,
) -> dict:
    """
    Compute all features for one (player, tournament-week) pair.

    pd_uniq is deduplicated by date so that players who played both sessions
    in the two-session era don't have their attendance rates or decay scores
    inflated relative to players in the single-session era.
    """
    n = len(pd_uniq)

    # ── Temporal ──────────────────────────────────────────────────────────────
    ts   = pd.Timestamp(tdate)
    mo_s = np.sin(2 * np.pi * ts.month / 12)
    mo_c = np.cos(2 * np.pi * ts.month / 12)
    wk   = ts.isocalendar().week
    wk_s = np.sin(2 * np.pi * wk / 52)
    wk_c = np.cos(2 * np.pi * wk / 52)

    days_since = int((tdate - pd_uniq[-1]) / np.timedelta64(1, "D"))

    # ── Attendance rates (lookback windows) ───────────────────────────────────
    # Denominator: distinct tournament weeks available in that window.
    # Numerator: distinct weeks where this player attended (any session).
    # Both counts use the same date-level granularity so the ratio is clean.
    i_td = int(np.searchsorted(week_dates_sorted, tdate, side="left"))

    def att_rate(n_weeks: int) -> float:
        cut     = tdate - np.timedelta64(int(n_weeks * 7), "D")
        n_avail = i_td - int(np.searchsorted(week_dates_sorted, cut, side="left"))
        n_played = n - int(np.searchsorted(pd_uniq, cut, side="left"))
        return n_played / max(n_avail, 1)

    # ── Decay score (matches bootstrap_mc_kalshi p_participate) ───────────────
    # Uses unique attendance dates so players who played both sessions in the
    # old era are treated the same as players who played one session.
    total_wk = max(int((tdate - start_ts) / np.timedelta64(7, "D")), 1)
    Z = (1.0 - PART_DECAY ** total_wk) / (1.0 - PART_DECAY)
    wdiffs = ((tdate - pd_uniq) / np.timedelta64(7, "D")).astype(float)
    decay = float(np.sum(PART_DECAY ** wdiffs) / Z)

    # ── Performance ───────────────────────────────────────────────────────────
    n_all = len(rp_)
    rec_n = min(8, n_all)
    rat   = float(ra_[-1])
    trend = float(np.mean(ra_[-4:]) - np.mean(ra_[-8:-4])) if n_all >= 8 else 0.0

    return {
        "month_sin":            mo_s,
        "month_cos":            mo_c,
        "week_sin":             wk_s,
        "week_cos":             wk_c,
        "log_weeks_since_last": np.log1p(days_since / 7.0),
        "att_rate_4w":          att_rate(4),
        "att_rate_8w":          att_rate(8),
        "att_rate_16w":         att_rate(16),
        "decay_score":          decay,
        "log_n_appearances":    np.log1p(n),
        "avg_rank_pct_recent":  float(np.mean(rp_[-rec_n:])),
        "avg_rank_pct_all":     float(np.mean(rp_)),
        "current_rating":       rat,
        "rating_trend":         trend,
    }


def build_dataset(df: pd.DataFrame, weekly_slots: pd.DataFrame) -> pd.DataFrame:
    """
    Build training rows: one per (player, tournament-week) in the player's
    active window. Label = 1 if they played any session that week, 0 if not.
    Features are computed from unique attendance dates strictly before the
    target week, so double-sessions in the old era don't distort the rates.
    """
    start_ts = np.datetime64(DATA_START, "D").astype("datetime64[ns]")

    week_dates     = weekly_slots["date"].values.astype("datetime64[ns]")
    week_dates_srt = np.sort(week_dates)  # already sorted, but be explicit

    n_users = df["username"].nunique()
    rows = []

    for u_idx, (username, hist) in enumerate(df.groupby("username")):
        if (u_idx + 1) % 500 == 0:
            print(f"    {u_idx + 1:,}/{n_users:,} players processed...")

        hist = hist.sort_values("date").reset_index(drop=True)

        # Unique weeks attended (deduplicated by date)
        attended_dates = set(hist["date"].values.astype("datetime64[ns]"))
        n_unique_weeks = len(attended_dates)
        if n_unique_weeks < MIN_APP:
            continue

        first_dt = hist["date"].iloc[0].to_datetime64()
        last_dt  = hist["date"].iloc[-1].to_datetime64()

        wm = (week_dates >= first_dt) & (week_dates <= last_dt)
        ps_dates = week_dates[wm]
        if len(ps_dates) == 0:
            continue

        # All appearance dates (may include duplicates for same-day multi-session)
        h_dates   = hist["date"].values.astype("datetime64[ns]")
        h_rp      = hist["rank_pct"].values.astype(float)
        h_ratings = hist["rating_num"].values.astype(float)

        ptr = 0  # two-pointer over all appearances (including multi-session)

        for tdate in ps_dates:
            while ptr < len(h_dates) and h_dates[ptr] < tdate:
                ptr += 1

            if ptr == 0:
                continue

            # Unique past attendance dates for attendance/decay features
            pd_uniq = np.unique(h_dates[:ptr])

            feats = _compute_features(
                tdate, pd_uniq, h_rp[:ptr], h_ratings[:ptr],
                week_dates_srt, start_ts,
            )
            feats["username"] = username
            feats["date"]     = pd.Timestamp(tdate)
            feats["label"]    = int(tdate in attended_dates)
            rows.append(feats)

    return pd.DataFrame(rows)


# ── Models ────────────────────────────────────────────────────────────────────

def make_models() -> dict:
    # Post-era dataset is ~45 weeks / ~50-100K rows — reduce capacity vs. full-history models.
    return {
        "logistic": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(
                C=1.0, max_iter=1000, class_weight="balanced", random_state=42,
            )),
        ]),
        "random_forest": RandomForestClassifier(
            n_estimators=200, max_depth=8, min_samples_leaf=20,
            class_weight="balanced", random_state=42, n_jobs=-1,
        ),
        "gradient_boost": GradientBoostingClassifier(
            n_estimators=150, max_depth=3, learning_rate=0.05,
            min_samples_leaf=20, subsample=0.8, random_state=42,
        ),
    }


# ── Evaluation ────────────────────────────────────────────────────────────────

def cross_validate(dataset: pd.DataFrame, models: dict) -> dict:
    """
    TimeSeriesSplit CV (4 folds) on the full post-era dataset.
    45 tournament weeks → 4 folds gives ~11 weeks per test split.
    """
    data = dataset.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date")
    print(f"  (CV on {len(data):,} rows, TimeSeriesSplit n_splits=4)")
    X = data[FEATURE_COLS].values
    y = data["label"].values

    tscv = TimeSeriesSplit(n_splits=4)
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
            "auc":      round(float(np.mean(aucs)),   4),
            "brier":    round(float(np.mean(briers)), 4),
            "log_loss": round(float(np.mean(lls)),    4),
        }
        print(f"  {name:<20}  AUC={cv_results[name]['auc']:.3f}  "
              f"Brier={cv_results[name]['brier']:.3f}  "
              f"LogLoss={cv_results[name]['log_loss']:.3f}")

    return cv_results


def train_final(dataset: pd.DataFrame, models: dict) -> dict:
    """Retrain each model on the full dataset."""
    data = dataset.dropna(subset=FEATURE_COLS + ["label"]).sort_values("date")
    X = data[FEATURE_COLS].values
    y = data["label"].values
    final = {}
    for name, model in models.items():
        model.fit(X, y)
        final[name] = model
        print(f"  {name} - trained on {len(y):,} rows")
    return final


# ── Persistence ───────────────────────────────────────────────────────────────

def save_models(final_models: dict, cv_results: dict) -> None:
    MODELS_DIR.mkdir(exist_ok=True)
    for name, model in final_models.items():
        path = MODELS_DIR / f"attendance_{name}.pkl"
        with open(path, "wb") as f:
            pickle.dump({
                "model":    model,
                "features": FEATURE_COLS,
                "cv":       cv_results.get(name, {}),
            }, f)
        print(f"  Saved -> {path}")


def load_models() -> dict:
    models = {}
    for path in sorted(MODELS_DIR.glob("attendance_*.pkl")):
        name = path.stem.replace("attendance_", "")
        with open(path, "rb") as f:
            models[name] = pickle.load(f)
        print(f"  Loaded {path}  (CV: {models[name].get('cv', {})})")
    return models


# ── Prediction ────────────────────────────────────────────────────────────────

def build_prediction_features(
    df: pd.DataFrame,
    weekly_slots: pd.DataFrame,
    target_date: pd.Timestamp,
) -> pd.DataFrame:
    """
    Build feature rows for all eligible players for a future tournament week.
    Uses all history strictly before target_date.
    """
    start_ts = np.datetime64(DATA_START, "D").astype("datetime64[ns]")
    tdate    = target_date.to_datetime64().astype("datetime64[ns]")

    week_dates_srt = np.sort(weekly_slots["date"].values.astype("datetime64[ns]"))

    rows = []
    for username, hist in df.groupby("username"):
        hist = hist.sort_values("date").reset_index(drop=True)

        h_dates   = hist["date"].values.astype("datetime64[ns]")
        h_rp      = hist["rank_pct"].values.astype(float)
        h_ratings = hist["rating_num"].values.astype(float)

        mask    = h_dates < tdate
        pd_uniq = np.unique(h_dates[mask])
        if len(pd_uniq) < MIN_APP:
            continue
        feats = _compute_features(
            tdate, pd_uniq, h_rp[mask], h_ratings[mask],
            week_dates_srt, start_ts,
        )
        feats["username"] = username
        feats["date"]     = target_date
        rows.append(feats)

    return pd.DataFrame(rows)


def predict_attendance(
    df: pd.DataFrame,
    weekly_slots: pd.DataFrame,
    models_payload: dict,
    target_date: pd.Timestamp,
) -> pd.DataFrame:
    """Return a DataFrame of all players sorted by ensemble P(attend)."""
    feat_df = build_prediction_features(df, weekly_slots, target_date)
    if feat_df.empty:
        print("No eligible players found.")
        return pd.DataFrame()

    X = feat_df[FEATURE_COLS].values
    out = feat_df[["username", "date"]].copy()

    for name, payload in models_payload.items():
        out[f"p_{name}"] = payload["model"].predict_proba(X)[:, 1]

    prob_cols = [c for c in out.columns if c.startswith("p_")]
    out["p_ensemble"] = out[prob_cols].mean(axis=1)
    return out.sort_values("p_ensemble", ascending=False).reset_index(drop=True)


def get_p_participate_for_mc(
    df: pd.DataFrame,
    weekly_slots: pd.DataFrame,
    models_payload: dict,
    target_date: pd.Timestamp,
    model_name: str = "gradient_boost",
) -> pd.Series:
    """
    Drop-in replacement for the p_participate Series used in bootstrap_mc_kalshi.py.
    Returns a Series indexed by username with P(player attends next tournament).
    Returns an empty Series if no eligible players exist (e.g. first post-era event).
    """
    preds = predict_attendance(df, weekly_slots, models_payload, target_date)
    if preds.empty:
        return pd.Series(dtype=float, name="p_participate")
    col = f"p_{model_name}" if f"p_{model_name}" in preds.columns else "p_ensemble"
    return preds.set_index("username")[col].rename("p_participate")


# ── Feature inspection ────────────────────────────────────────────────────────

def print_feature_importances(final_models: dict) -> None:
    print("\n-- Logistic Regression Coefficients (by |coef|) -----------------")
    pipe = final_models["logistic"]
    coefs = pipe.named_steps["clf"].coef_[0]
    for feat, coef in sorted(zip(FEATURE_COLS, coefs), key=lambda x: abs(x[1]), reverse=True):
        bar = "#" * int(abs(coef) * 20)
        sign = "+" if coef > 0 else "-"
        print(f"  {feat:<28} {sign}{abs(coef):.4f}  {bar}")

    print("\n-- Random Forest Feature Importances ----------------------------")
    rf = final_models["random_forest"]
    for feat, imp in sorted(
        zip(FEATURE_COLS, rf.feature_importances_), key=lambda x: x[1], reverse=True
    ):
        bar = "#" * int(imp * 100)
        print(f"  {feat:<28} {imp:.4f}  {bar}")

    print("\n-- Gradient Boost Feature Importances ---------------------------")
    gb = final_models["gradient_boost"]
    for feat, imp in sorted(
        zip(FEATURE_COLS, gb.feature_importances_), key=lambda x: x[1], reverse=True
    ):
        bar = "#" * int(imp * 100)
        print(f"  {feat:<28} {imp:.4f}  {bar}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--predict", action="store_true",
                        help="Load saved models and predict instead of training")
    parser.add_argument("--date",    type=str, default=None,
                        help="Target date YYYY-MM-DD (default: next Tuesday after last data)")
    parser.add_argument("--top",     type=int, default=30,
                        help="Print top N players (default: 30)")
    args = parser.parse_args()

    print("Loading data...")
    df = load_data()
    weekly_slots = get_weekly_slots(df)
    print(f"  {df['username'].nunique():,} players | "
          f"{df['date'].min().date()} to {df['date'].max().date()}")
    print(f"  {len(weekly_slots):,} tournament weeks")

    # ── Determine target date ─────────────────────────────────────────────────
    if args.date:
        target_date = pd.Timestamp(args.date)
    else:
        last_date  = df["date"].max()
        days_ahead = (1 - last_date.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        target_date = last_date + pd.Timedelta(days=days_ahead)

    # ── Predict mode ──────────────────────────────────────────────────────────
    if args.predict:
        print("\nLoading saved models...")
        models_payload = load_models()
        if not models_payload:
            print("No saved models found in models/. Run without --predict first.")
            return

        print(f"\nPredicting attendance for {target_date.date()}...")
        results = predict_attendance(df, weekly_slots, models_payload, target_date)

        print(f"\nTop {args.top} by ensemble P(attend):")
        print(results.head(args.top).to_string(index=False,
                                                float_format=lambda x: f"{x:.3f}"))

        out_path = MODELS_DIR / f"predictions_{target_date.date()}.csv"
        results.to_csv(out_path, index=False)
        print(f"\nFull predictions saved -> {out_path}")
        return

    # ── Train mode ────────────────────────────────────────────────────────────
    # Cache name includes era start date to distinguish from pre-era caches.
    dataset_cache = MODELS_DIR / "dataset_cache_post2025.parquet"
    MODELS_DIR.mkdir(exist_ok=True)
    if dataset_cache.exists():
        print("\nLoading cached training dataset...")
        dataset = pd.read_parquet(dataset_cache)
    else:
        print("\nBuilding training dataset...")
        dataset = build_dataset(df, weekly_slots)
        dataset.to_parquet(dataset_cache, index=False)
        print(f"  Cached to {dataset_cache}")

    n_pos = int(dataset["label"].sum())
    n_tot = len(dataset)
    print(f"  {n_tot:,} observations | "
          f"label=1: {n_pos:,} ({n_pos/n_tot:.1%}) | "
          f"label=0: {n_tot-n_pos:,} ({(n_tot-n_pos)/n_tot:.1%})")
    print(f"  {dataset['username'].nunique():,} unique players in dataset")

    print("\nCross-validating (TimeSeriesSplit, 4 folds)...")
    models = make_models()
    cv_results = cross_validate(dataset, models)

    print("\nTraining final models on all data...")
    final_models = train_final(dataset, models)

    print("\nSaving models...")
    save_models(final_models, cv_results)

    print_feature_importances(final_models)

    print(f"\nNext prediction target: {target_date.date()}")
    print("Run with --predict to generate attendance probabilities.")
    print("\nDone.")


if __name__ == "__main__":
    main()
