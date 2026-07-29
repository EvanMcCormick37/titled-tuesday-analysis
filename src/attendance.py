"""
Opportunism adjustment for TT attendance — SIDE MODULE.

This module is no longer called from the main prediction pipeline.
Attendance adjustments (cuts, conflict caps, nudges) are now stored in the
attendance_adjustments DB table and applied by src.data.load_and_prepare()
when a tourn_date is provided.

This module is preserved for ad-hoc analysis.  Call
apply_schedule_adjustments() directly from a notebook if you want to
experiment with the opportunism boost on top of the main predictions.

Original three-step logic:
  1. Hard cut (cut_players)         → p_participate = 0
  2. Conflict-rate cap              → p = min(ewma, historical_conflict_rate)
  3. Opportunism boost              → p += shrunk_slope × n_top_conflicted
"""

import sqlite3
import unicodedata
from collections import defaultdict

import numpy as np
import pandas as pd

from .config import DB_PATH


# ── Name normalisation ────────────────────────────────────────────────────────

def _norm_name(s: str) -> str:
    """'Last, First' → 'first last', strip diacritics, lowercase."""
    if not s:
        return ''
    if ',' in s:
        last, first = s.split(',', 1)
        s = f"{first.strip()} {last.strip()}"
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return ' '.join(s.lower().split())


# ── Name → username resolution ────────────────────────────────────────────────

def _resolve_player_names(player_names: list[str]) -> dict[str, str]:
    """Map player_information.player_name values to chess.com usernames.

    Tries exact normalized match first, then word-set containment for PGN
    names (e.g. 'Sarin, Nihal' → 'nihalsarin').
    Returns dict[player_name → username] for matched names only.
    """
    conn = sqlite3.connect(DB_PATH)
    pi_rows = conn.execute(
        'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL'
    ).fetchall()
    conn.close()

    exact: dict[str, str] = {}
    prefix_list: list[tuple[str, str]] = []
    for uname, pname in pi_rows:
        n = _norm_name(pname)
        if n not in exact:
            exact[n] = uname
        prefix_list.append((n, uname))
    prefix_list.sort(key=lambda x: -len(x[0]))

    result: dict[str, str] = {}
    for player_name in player_names:
        n = _norm_name(player_name)
        if n in exact:
            result[player_name] = exact[n]
            continue
        oep_words = set(n.split())
        for known_norm, uname in prefix_list:
            known_words = set(known_norm.split())
            if len(known_words) >= 2 and known_words <= oep_words:
                result[player_name] = uname
                break
    return result


# ── Conflict-rate computation ─────────────────────────────────────────────────

def _compute_conflict_rates(
    cut_usernames: list[str],
    account_groups: dict[str, list[str]],
) -> dict[str, float]:
    """Historical TT attendance rate for each cut player on conflict TT dates.

    Returns dict[username → rate].  Players with fewer than 3 conflict dates
    are omitted (insufficient data).
    """
    if not cut_usernames:
        return {}

    conn = sqlite3.connect(DB_PATH)
    tt_dates = {row[0][:10] for row in conn.execute(
        'SELECT DISTINCT date FROM titled_tuesday_standings'
    ).fetchall()}

    cut_set = set(cut_usernames)
    pi_rows = conn.execute(
        'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL'
    ).fetchall()

    exact: dict[str, str] = {}
    prefix_list: list[tuple[str, str]] = []
    for uname, pname in pi_rows:
        if uname not in cut_set:
            continue
        n = _norm_name(pname)
        exact[n] = uname
        prefix_list.append((n, uname))
    prefix_list.sort(key=lambda x: -len(x[0]))

    def _lookup(oep_name: str):
        n = _norm_name(oep_name)
        if n in exact:
            return exact[n]
        oep_words = set(n.split())
        for known_norm, uname in prefix_list:
            known_words = set(known_norm.split())
            if len(known_words) >= 2 and known_words <= oep_words:
                return uname
        return None

    oep_rows = conn.execute(
        'SELECT broadcast_name, player_name FROM other_event_participants'
    ).fetchall()
    event_to_cut: dict[str, set] = defaultdict(set)
    for bname, pname in oep_rows:
        u = _lookup(pname)
        if u:
            event_to_cut[bname].add(u)

    oer_rows = conn.execute(
        'SELECT broadcast_name, date(earliest_start_utc) FROM other_event_rounds'
    ).fetchall()
    user_round_dates: dict[str, set] = defaultdict(set)
    for bname, rdate in oer_rows:
        for u in event_to_cut.get(bname, ()):
            user_round_dates[u].add(rdate)

    result: dict[str, float] = {}
    for username in cut_usernames:
        conflict_dates = sorted(user_round_dates[username] & tt_dates)
        n_c = len(conflict_dates)
        if n_c < 3:
            print(f'  {username}: only {n_c} conflict TT date(s) - leaving p_participate unchanged')
            continue
        all_accts = account_groups.get(username, [username])
        acc_ph = ','.join('?' * len(all_accts))
        dt_ph  = ','.join('?' * n_c)
        n_att  = conn.execute(
            f'SELECT COUNT(DISTINCT date(date)) FROM titled_tuesday_standings '
            f'WHERE username IN ({acc_ph}) AND date(date) IN ({dt_ph})',
            all_accts + conflict_dates,
        ).fetchone()[0]
        rate = n_att / n_c
        result[username] = rate
        note = f' (combined {len(all_accts)} accounts)' if len(all_accts) > 1 else ''
        print(f'  {username}{note}: {n_att}/{n_c} conflict TTs -> conflict_rate={rate:.3f}')

    conn.close()
    return result


# ── Broadcast conflict map ────────────────────────────────────────────────────

def _compute_broadcast_conflict_map() -> dict[str, set]:
    """Return dict[username → set[date_str]] of broadcast-round dates per player.

    Uses exact normalized name matching only (no fuzzy fallback).  Fast because
    other_event_participants has 450K+ rows — the fuzzy word-set scan would be
    prohibitively slow at that scale.  Exact matching covers the vast majority
    of identifiable players for the opportunism slope computation.
    """
    conn = sqlite3.connect(DB_PATH)
    pi_rows = conn.execute(
        'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL'
    ).fetchall()
    exact: dict[str, str] = {}
    for uname, pname in pi_rows:
        n = _norm_name(pname)
        if n not in exact:
            exact[n] = uname

    oep_rows = conn.execute(
        'SELECT broadcast_name, player_name FROM other_event_participants'
    ).fetchall()
    event_to_users: dict[str, set] = defaultdict(set)
    for bname, pname in oep_rows:
        u = exact.get(_norm_name(pname))
        if u:
            event_to_users[bname].add(u)

    oer_rows = conn.execute(
        'SELECT broadcast_name, date(earliest_start_utc) FROM other_event_rounds'
    ).fetchall()
    conflict_map: dict[str, set] = defaultdict(set)
    for bname, rdate in oer_rows:
        for u in event_to_users.get(bname, ()):
            conflict_map[u].add(rdate)

    conn.close()
    return dict(conflict_map)


# ── Opportunism slopes ────────────────────────────────────────────────────────

def _compute_opportunism_slopes(
    top_usernames: set,
    conflict_map: dict,
    min_obs: int,
) -> pd.DataFrame:
    """OLS slope of P(attend) ~ n_top_conflicted on non-conflict TT dates per player.

    Returns DataFrame[username, slope, se, n_obs].
    Uses the full TT history (no DATA_CUTOFF) for maximum regression window.
    Skips players with <min_obs eligible dates or a constant outcome.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT username, date(date) AS date FROM titled_tuesday_standings'
    ).fetchall()
    conn.close()

    attended: set = set(rows)
    tt_dates = sorted({d for _, d in attended})

    career: dict[str, tuple] = {}
    for u, d in attended:
        p = career.get(u)
        career[u] = (d, d) if p is None else (min(p[0], d), max(p[1], d))

    hist_ntc = {
        d: sum(1 for u in top_usernames if d in conflict_map.get(u, set()))
        for d in tt_dates
    }

    results = []
    for username, (first, last) in career.items():
        conf = conflict_map.get(username, set())
        elig = [d for d in tt_dates if first <= d <= last and d not in conf]
        if len(elig) < min_obs:
            continue

        x = np.array([hist_ntc[d] for d in elig], dtype=float)
        y = np.array([1.0 if (username, d) in attended else 0.0 for d in elig])

        if np.var(x, ddof=1) < 1e-6 or y.std() < 1e-6:
            continue

        n     = len(elig)
        slope = np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1)
        y_hat = np.mean(y) + slope * (x - np.mean(x))
        sig   = np.sqrt(max(np.sum((y - y_hat) ** 2) / (n - 2), 0.0))
        se    = sig / (np.std(x, ddof=1) * np.sqrt(n))

        results.append({'username': username, 'slope': slope, 'se': se, 'n_obs': n})

    return (pd.DataFrame(results) if results
            else pd.DataFrame(columns=['username', 'slope', 'se', 'n_obs']))


def _shrink_slopes(df_slopes: pd.DataFrame) -> pd.DataFrame:
    """DerSimonian-Laird empirical-Bayes shrinkage toward grand mean.

    Shrunk slopes are floored at 0: opportunism can only raise attendance.
    (A negative grand mean reflects tournament-season confounding, not a causal
    anti-opportunism effect.)
    """
    df     = df_slopes.copy()
    slopes = df['slope'].values
    ses    = df['se'].values
    k      = len(slopes)

    w  = 1.0 / np.maximum(ses ** 2, 1e-12)
    W  = w.sum()
    mu = np.dot(w, slopes) / W

    Q    = np.dot(w, (slopes - mu) ** 2)
    C    = W - np.dot(w ** 2, np.ones(k)) / W
    tau2 = max(0.0, (Q - (k - 1)) / C)
    I2   = max(0.0, (Q - (k - 1)) / Q) * 100 if Q > 0 else 0.0

    if tau2 == 0.0:
        raw, med_B = np.full(k, mu), 1.0
    else:
        B          = ses ** 2 / (ses ** 2 + tau2)
        raw, med_B = mu + (1.0 - B) * (slopes - mu), float(np.median(B))

    df['shrunk_slope'] = np.maximum(raw, 0.0)
    print(f'  Shrinkage: grand_mean={mu:.4f}  tau={np.sqrt(tau2):.4f}  '
          f'Q={Q:.0f}  I2={I2:.0f}%  median_shrinkage={med_B:.2f}')
    return df


# ── Public entry point ────────────────────────────────────────────────────────

def apply_schedule_adjustments(
    p_participate: pd.Series,
    scheduling_conflict: list[str],
    cut_players: list[str] | None = None,
    canonical_accounts: dict[str, str] | None = None,
    account_groups: dict[str, list[str]] | None = None,
    top_player_threshold: float = 0.10,
    min_obs_opportunism: int = 20,
) -> pd.Series:
    """Apply broadcast-conflict and opportunism adjustments to p_participate.

    Parameters
    ----------
    p_participate        Series[username → float] from load_and_prepare().
    scheduling_conflict  Player names (player_information.player_name) with a
                         broadcast round on the upcoming TT Tuesday.
    cut_players          Player names with an unavoidable conflict; p_participate
                         is set to 0 for these players.
    canonical_accounts   Maps closed/alt username → active canonical username
                         (e.g. {'IMHansNiemann': 'HansOnTwitch'}).
    account_groups       Maps canonical username → list of all chess.com accounts
                         for the same player (for attendance aggregation).
    top_player_threshold P_top10_given_play threshold to count as a 'top player'
                         for n_top_conflicted (read from latest_model_predictions_raw;
                         run make_predictions.py first or this will raise).
    min_obs_opportunism  Minimum non-conflict TT dates required to estimate a slope.

    Returns a modified copy of p_participate.
    """
    if canonical_accounts is None:
        canonical_accounts = {}
    if account_groups is None:
        account_groups = {}
    if cut_players is None:
        cut_players = []

    p = p_participate.copy()

    if not scheduling_conflict and not cut_players:
        return p

    # ── Resolve scheduling_conflict names → canonical usernames ───────────────
    sc_name_map = _resolve_player_names(scheduling_conflict) if scheduling_conflict else {}
    sc_missing  = [pl for pl in scheduling_conflict if pl not in sc_name_map]
    if sc_missing:
        print(f'  Warning: no player_information entry for: {sc_missing}')

    sc_raw = [sc_name_map[pl] for pl in scheduling_conflict if pl in sc_name_map]
    sc_usernames = list(dict.fromkeys(canonical_accounts.get(u, u) for u in sc_raw))

    # ── Resolve cut_players names → canonical usernames ───────────────────────
    cut_name_map = _resolve_player_names(cut_players) if cut_players else {}
    cut_missing  = [pl for pl in cut_players if pl not in cut_name_map]
    if cut_missing:
        print(f'  Warning: no player_information entry for: {cut_missing}')

    cut_raw = [cut_name_map[pl] for pl in cut_players if pl in cut_name_map]
    cut_usernames = list(dict.fromkeys(canonical_accounts.get(u, u) for u in cut_raw))

    for old, canonical in canonical_accounts.items():
        if old not in p.index:
            continue
        if canonical in sc_usernames or canonical in cut_usernames:
            p[old] = 0.0

    # ── Hard cut: unavoidable conflicts → p_participate = 0 ───────────────────
    for username in cut_usernames:
        if username in p.index:
            print(f'  {username}: cut (unavoidable conflict) -> p_participate=0.000')
            p[username] = 0.0

    # ── Conflict-rate cap for scheduling_conflict players ─────────────────────
    if sc_usernames:
        print('Computing conflict-conditional attendance rates...')
        for username, rate in _compute_conflict_rates(sc_usernames, account_groups).items():
            if username not in p.index:
                continue
            ewma = p[username]
            p[username] = min(ewma, rate)
            print(f'  {username}: EWMA={ewma:.3f}  conflict={rate:.3f}  -> p_participate={p[username]:.3f}')

    # ── Opportunism boost ──────────────────────────────────────────────────────
    all_conflicted = list(dict.fromkeys(sc_usernames + cut_usernames))

    conn = sqlite3.connect(DB_PATH)
    pred_rows = conn.execute(
        'SELECT username, P_top10_given_play FROM latest_model_predictions_raw'
    ).fetchall()
    conn.close()

    top_usernames = {u for u, pv in pred_rows if pv is not None and pv >= top_player_threshold}

    if not top_usernames:
        print('Opportunism: skipping (latest_model_predictions_raw is empty)')
        return p

    n_top_conflicted = sum(1 for u in all_conflicted if u in top_usernames)
    print(f'Opportunism: n_top_conflicted={n_top_conflicted}  '
          f'({len(top_usernames)} top players, threshold P_top10>{top_player_threshold})')

    print('  Building broadcast conflict map...')
    conflict_map = _compute_broadcast_conflict_map()

    print('  Computing per-player opportunism slopes...')
    df_slopes = _compute_opportunism_slopes(top_usernames, conflict_map, min_obs_opportunism)
    print(f'  Slopes computed for {len(df_slopes):,} players')

    if df_slopes.empty:
        return p

    df_slopes = _shrink_slopes(df_slopes)
    slope_map = dict(zip(df_slopes['username'], df_slopes['shrunk_slope']))

    all_conflicted_set = set(all_conflicted)
    if n_top_conflicted > 0:
        n_adj = 0
        for username in p.index:
            if username in all_conflicted_set:
                continue
            shrunk = slope_map.get(username, 0.0)
            if shrunk < 1e-9:
                continue
            p[username] = float(np.clip(p[username] + shrunk * n_top_conflicted, 0.0, 1.0))
            n_adj += 1
        print(f'  p_participate adjusted for {n_adj:,} players')
    else:
        print('  n_top_conflicted=0 -> no opportunism adjustment applied')

    return p
