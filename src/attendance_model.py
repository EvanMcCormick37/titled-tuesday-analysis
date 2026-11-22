"""Two-part attendance: P(attend) = P(available) x P(chooses to play | available).

The EWMA in data.load_and_prepare treats every absence alike. Here the choice part
is a logistic regression fitted, walk-forward, only on weeks where the player had
no known conflict (attendance_conflicts), so a forced absence is not read as lost
interest. Its inputs, all from events before the one predicted:

  - a fast and a slow decayed attendance rate (conflict weeks left out of the
    denominator when deduct_conflicts is set)
  - weeks since the player last played
  - how many events they have played
  - the share of past events on which they had a conflict

P(available) for the week itself is not modelled here: a known week-of conflict is
still applied as a manual cut (or found by the attendance researcher). Selected per
market with ModelConfig.attendance_model = 'two_part'.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DECAY_FAST = 0.6
DECAY_SLOW = 0.97
TRAIN_EVENTS = 150      # most recent events used as training targets
WARMUP_EVENTS = 5       # events of history before an event is used as a target
MIN_TRAIN_ROWS = 200
_FEATURES = ('logit_fast', 'logit_slow', 'log_weeks_since', 'log_appearances', 'conflict_rate')


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 0.005, 0.995)
    return np.log(p / (1 - p))


def _features(fast, slow, since, count, conflicts, t) -> np.ndarray:
    return np.column_stack([
        _logit(fast[0] / np.maximum(fast[1], 1e-9)),
        _logit(slow[0] / np.maximum(slow[1], 1e-9)),
        np.log1p(since),
        np.log1p(count),
        conflicts / max(t, 1),
    ])


def two_part_participation(df: pd.DataFrame, ref: pd.Timestamp, conflicts: set[tuple[str, str]],
                           deduct_conflicts: bool = True, min_date: pd.Timestamp | None = None,
                           min_rate: float = 0.005) -> pd.Series | None:
    """P(chooses to play | available) at `ref` for every player with a past appearance.

    `df` holds standings rows before `ref` with tz-naive `date` and `username`.
    `conflicts` holds (YYYY-MM-DD, username) pairs. Returns None when there is too
    little history to fit, so the caller can keep its EWMA.
    """
    from sklearn.linear_model import LogisticRegression

    rows = df[['date', 'username']].copy()
    if min_date is not None:
        rows = rows[rows['date'] >= min_date]
    dates = np.array(sorted(rows['date'].unique()))
    if len(dates) <= WARMUP_EVENTS:
        return None
    users = np.array(sorted(rows['username'].unique()))
    u_index = {u: i for i, u in enumerate(users)}
    d_index = {d: j for j, d in enumerate(dates)}
    attended = np.zeros((len(users), len(dates)), dtype=bool)
    attended[rows['username'].map(u_index).to_numpy(), rows['date'].map(d_index).to_numpy()] = True
    day = {pd.Timestamp(d).strftime('%Y-%m-%d'): j for j, d in enumerate(dates)}
    conflicted = np.zeros_like(attended)
    for d, u in conflicts:
        if d in day and u in u_index:
            conflicted[u_index[u], day[d]] = True

    week = pd.Timedelta(weeks=1)
    targets = list(dates) + [ref]
    fast = [np.zeros(len(users)), np.zeros(len(users))]     # decayed numerator, denominator
    slow = [np.zeros(len(users)), np.zeros(len(users))]
    last_seen = np.full(len(users), np.nan)                  # weeks index of the last appearance
    count = np.zeros(len(users))
    n_conflicts = np.zeros(len(users))
    first_target = max(WARMUP_EVENTS, len(dates) - TRAIN_EVENTS)
    X_train, y_train = [], []

    for t, target in enumerate(targets):
        if t > 0:
            gap = (pd.Timestamp(target) - pd.Timestamp(dates[t - 1])) // week
            x = attended[:, t - 1].astype(float)
            counted = (~conflicted[:, t - 1]).astype(float) if deduct_conflicts else np.ones(len(users))
            for state, decay in ((fast, DECAY_FAST), (slow, DECAY_SLOW)):
                state[0] = (state[0] + x) * decay ** gap
                state[1] = (state[1] + counted) * decay ** gap
            count += x
            n_conflicts += conflicted[:, t - 1]
            last_seen[attended[:, t - 1]] = (pd.Timestamp(dates[t - 1]) - pd.Timestamp(dates[0])) // week
        now = (pd.Timestamp(target) - pd.Timestamp(dates[0])) // week
        known = count > 0
        if t < len(dates) and t < first_target:
            continue
        feats = _features(fast, slow, now - last_seen, count, n_conflicts, t)
        if t < len(dates):
            usable = known & ~conflicted[:, t]
            X_train.append(feats[usable])
            y_train.append(attended[usable, t])
        else:
            X_pred, pred_users = feats[known], users[known]

    if not X_train:
        return None
    X, y = np.vstack(X_train), np.concatenate(y_train)
    if len(y) < MIN_TRAIN_ROWS or y.all() or not y.any():
        return None
    model = LogisticRegression(C=1.0, max_iter=1000).fit(X, y)
    p = model.predict_proba(X_pred)[:, 1]
    coefs = ', '.join(f'{name}={c:+.2f}' for name, c in zip(_FEATURES, model.coef_[0]))
    print(f'  p_participate: two-part model fitted on {len(y):,} conflict-free player-weeks ({coefs})')
    return pd.Series(np.clip(p, min_rate, 1.0), index=pred_users, name='p_participate')
