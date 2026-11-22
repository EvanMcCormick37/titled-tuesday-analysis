"""Market-maker tables: control, player overrides, alerts, status and fills.

Created automatically by `init_schema()` (tt-api, tt-worker and tt-mm call it at
startup), like the trade-queue tables. They are new tables, so no migration of
existing data is needed.

    mm_control           one row per market: paused flag + parameter overrides (JSON)
    mm_player_overrides  per (market, event, username): fallback factors, wipes, pull state
    mm_alerts            anchor fills, fallbacks, pulls, holds, cutoff confirmations
    mm_status            one row per market, rewritten by tt-mm every few seconds
    mm_fills             every fill on a market-maker order, tagged with its tier
"""
from __future__ import annotations

import json
from typing import Any

from ..db import get_conn, is_postgres
from ..job_queue import _loads, _norm_ts, iso, utcnow
from .config import MMParams


def _ddl() -> list[str]:
    pk, ts, js = (
        ('BIGSERIAL PRIMARY KEY', 'TIMESTAMPTZ', 'JSONB') if is_postgres()
        else ('INTEGER PRIMARY KEY AUTOINCREMENT', 'TEXT', 'TEXT')
    )
    return [
        f"""
        CREATE TABLE IF NOT EXISTS mm_control (
            market      TEXT PRIMARY KEY,
            paused      INTEGER NOT NULL DEFAULT 0,
            reason      TEXT,
            params      {js},
            updated_at  {ts},
            updated_by  TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS mm_player_overrides (
            market          TEXT NOT NULL,
            tourn_date      TEXT NOT NULL,
            username        TEXT NOT NULL,
            player_name     TEXT,
            status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','pulled')),
            yes_factor      REAL NOT NULL DEFAULT 1.0,
            no_factor       REAL NOT NULL DEFAULT 1.0,
            wipes           INTEGER NOT NULL DEFAULT 0,
            markup_override REAL,
            fv_version      TEXT,
            updated_at      {ts},
            updated_by      TEXT,
            PRIMARY KEY (market, tourn_date, username)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS mm_alerts (
            id           {pk},
            market       TEXT NOT NULL,
            tourn_date   TEXT,
            ts           {ts} NOT NULL,
            level        TEXT NOT NULL DEFAULT 'info',
            kind         TEXT NOT NULL,
            username     TEXT,
            ticker       TEXT,
            message      TEXT NOT NULL,
            data         {js},
            acknowledged INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_mm_alerts_market ON mm_alerts(market, id)",
        f"""
        CREATE TABLE IF NOT EXISTS mm_status (
            market        TEXT PRIMARY KEY,
            heartbeat_at  {ts},
            mode          TEXT,
            paused_ack    INTEGER NOT NULL DEFAULT 0,
            state         {js}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS mm_fills (
            trade_id        TEXT PRIMARY KEY,
            market          TEXT NOT NULL,
            tourn_date      TEXT,
            ticker          TEXT NOT NULL,
            side            TEXT NOT NULL,
            tier            TEXT NOT NULL,
            price_c         INTEGER NOT NULL,
            count           REAL NOT NULL,
            username        TEXT,
            client_order_id TEXT,
            ts              {ts} NOT NULL
        )
        """,
    ]


def init_schema() -> None:
    with get_conn() as conn:
        for stmt in _ddl():
            conn.execute(stmt)


# ── Control ──────────────────────────────────────────────────────────────────

def get_control(market: str) -> dict:
    with get_conn() as conn:
        row = conn.execute('SELECT paused, reason, params, updated_at, updated_by FROM mm_control '
                           'WHERE market = ?', (market,)).fetchone()
    if row is None:
        return {'paused': False, 'reason': None, 'params': {}, 'updated_at': None, 'updated_by': None}
    return {'paused': bool(row[0]), 'reason': row[1], 'params': _loads(row[2]) or {},
            'updated_at': _norm_ts(row[3]), 'updated_by': row[4]}


def params_for(market: str, control: dict | None = None) -> MMParams:
    control = control if control is not None else get_control(market)
    return MMParams(**(control.get('params') or {}))


def _upsert_control(conn: Any, market: str, **cols: Any) -> None:
    now = iso(utcnow())
    exists = conn.execute('SELECT 1 FROM mm_control WHERE market = ?', (market,)).fetchone()
    if exists:
        sets = ', '.join(f'{k} = ?' for k in cols)
        conn.execute(f'UPDATE mm_control SET {sets}, updated_at = ? WHERE market = ?',
                     (*cols.values(), now, market))
    else:
        names = ', '.join(cols)
        marks = ', '.join('?' for _ in cols)
        conn.execute(f'INSERT INTO mm_control (market, {names}, updated_at) VALUES (?, {marks}, ?)',
                     (market, *cols.values(), now))


def set_paused(market: str, paused: bool, reason: str | None = None,
               by: str | None = None, conn: Any = None) -> None:
    if conn is not None:
        _upsert_control(conn, market, paused=int(paused), reason=reason, updated_by=by)
        return
    with get_conn() as c:
        _upsert_control(c, market, paused=int(paused), reason=reason, updated_by=by)


def set_params(market: str, params: dict, by: str | None = None) -> MMParams:
    validated = MMParams(**params)
    overrides = validated.model_dump(exclude_defaults=True)
    with get_conn() as conn:
        _upsert_control(conn, market, params=json.dumps(overrides), updated_by=by)
    return validated


# ── Player overrides ─────────────────────────────────────────────────────────

_OVR_COLS = ('username', 'player_name', 'status', 'yes_factor', 'no_factor', 'wipes',
             'markup_override', 'fv_version', 'updated_at', 'updated_by')


def get_overrides(market: str, tourn_date: str) -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute(f'SELECT {", ".join(_OVR_COLS)} FROM mm_player_overrides '
                            'WHERE market = ? AND tourn_date = ?', (market, tourn_date)).fetchall()
    out = {}
    for r in rows:
        d = dict(zip(_OVR_COLS, r))
        d['updated_at'] = _norm_ts(d['updated_at'])
        out[d['username']] = d
    return out


def upsert_override(market: str, tourn_date: str, username: str, *, by: str = 'mm', **cols: Any) -> None:
    now = iso(utcnow())
    with get_conn() as conn:
        exists = conn.execute('SELECT 1 FROM mm_player_overrides WHERE market = ? AND tourn_date = ? '
                              'AND username = ?', (market, tourn_date, username)).fetchone()
        if exists:
            sets = ', '.join(f'{k} = ?' for k in cols)
            conn.execute(f'UPDATE mm_player_overrides SET {sets}, updated_at = ?, updated_by = ? '
                         'WHERE market = ? AND tourn_date = ? AND username = ?',
                         (*cols.values(), now, by, market, tourn_date, username))
        else:
            names = ''.join(f', {k}' for k in cols)
            marks = ''.join(', ?' for _ in cols)
            conn.execute(f'INSERT INTO mm_player_overrides (market, tourn_date, username{names}, '
                         f'updated_at, updated_by) VALUES (?, ?, ?{marks}, ?, ?)',
                         (market, tourn_date, username, *cols.values(), now, by))


def clear_factors(market: str, tourn_date: str, fv_version: str) -> int:
    """A new model run supersedes the automatic fallback shifts, but not pulls."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE mm_player_overrides SET yes_factor = 1.0, no_factor = 1.0, wipes = 0, "
            "fv_version = ?, updated_at = ?, updated_by = 'model_run' "
            "WHERE market = ? AND tourn_date = ? AND status = 'active' "
            "AND (yes_factor <> 1.0 OR no_factor <> 1.0 OR wipes <> 0)",
            (fv_version, iso(utcnow()), market, tourn_date))
        return cur.rowcount or 0


def repost_player(market: str, tourn_date: str, username: str, markup: float | None,
                  by: str = 'dashboard') -> None:
    upsert_override(market, tourn_date, username, by=by, status='active', yes_factor=1.0,
                    no_factor=1.0, wipes=0, markup_override=markup)


def pull_player(market: str, tourn_date: str, username: str, by: str = 'dashboard') -> None:
    upsert_override(market, tourn_date, username, by=by, status='pulled')


# ── Alerts ───────────────────────────────────────────────────────────────────

def add_alert(market: str, kind: str, message: str, *, tourn_date: str | None = None,
              level: str = 'info', username: str | None = None, ticker: str | None = None,
              data: dict | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO mm_alerts (market, tourn_date, ts, level, kind, username, ticker, message, data) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (market, tourn_date, iso(utcnow()), level, kind, username, ticker, message,
             json.dumps(data) if data is not None else None))


_ALERT_COLS = ('id', 'market', 'tourn_date', 'ts', 'level', 'kind', 'username', 'ticker',
               'message', 'data', 'acknowledged')


def list_alerts(market: str, limit: int = 100, unacked_only: bool = False) -> list[dict]:
    where = 'WHERE market = ?' + (' AND acknowledged = 0' if unacked_only else '')
    with get_conn() as conn:
        rows = conn.execute(f'SELECT {", ".join(_ALERT_COLS)} FROM mm_alerts {where} '
                            'ORDER BY id DESC LIMIT ?', (market, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(_ALERT_COLS, r))
        d['ts'] = _norm_ts(d['ts'])
        d['data'] = _loads(d['data'])
        d['acknowledged'] = bool(d['acknowledged'])
        out.append(d)
    return out


def ack_alert(alert_id: int) -> bool:
    with get_conn() as conn:
        return (conn.execute('UPDATE mm_alerts SET acknowledged = 1 WHERE id = ?',
                             (alert_id,)).rowcount or 0) > 0


def ack_all_alerts(market: str) -> int:
    """Acknowledge every open alert for a market. Returns how many were open."""
    with get_conn() as conn:
        return conn.execute('UPDATE mm_alerts SET acknowledged = 1 WHERE market = ? AND acknowledged = 0',
                            (market,)).rowcount or 0


# ── Status ───────────────────────────────────────────────────────────────────

def write_status(market: str, mode: str, paused_ack: bool, state: dict) -> None:
    now = iso(utcnow())
    payload = json.dumps(state, default=str)
    with get_conn() as conn:
        if conn.execute('SELECT 1 FROM mm_status WHERE market = ?', (market,)).fetchone():
            conn.execute('UPDATE mm_status SET heartbeat_at = ?, mode = ?, paused_ack = ?, state = ? '
                         'WHERE market = ?', (now, mode, int(paused_ack), payload, market))
        else:
            conn.execute('INSERT INTO mm_status (market, heartbeat_at, mode, paused_ack, state) '
                         'VALUES (?, ?, ?, ?, ?)', (market, now, mode, int(paused_ack), payload))


def get_status(market: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute('SELECT heartbeat_at, mode, paused_ack, state FROM mm_status WHERE market = ?',
                           (market,)).fetchone()
    if row is None:
        return None
    return {'heartbeat_at': _norm_ts(row[0]), 'mode': row[1], 'paused_ack': bool(row[2]),
            'state': _loads(row[3]) or {}}


def heartbeat_age(market: str) -> float | None:
    from datetime import datetime
    st = get_status(market)
    if not st or not st['heartbeat_at']:
        return None
    return (utcnow() - datetime.fromisoformat(st['heartbeat_at'])).total_seconds()


def is_quoting_live(market: str, stale_after_s: float = 20) -> bool:
    """True when a live, unpaused tt-mm with a fresh heartbeat is quoting this market."""
    st = get_status(market)
    if not st or st['mode'] != 'live' or st['state'].get('paused'):
        return False
    age = heartbeat_age(market)
    return age is not None and age <= stale_after_s


# ── Fills ────────────────────────────────────────────────────────────────────

def record_fill(market: str, tourn_date: str | None, fill: dict) -> bool:
    """Insert once per trade_id. Returns False if it was already recorded."""
    with get_conn() as conn:
        if conn.execute('SELECT 1 FROM mm_fills WHERE trade_id = ?', (fill['trade_id'],)).fetchone():
            return False
        conn.execute(
            'INSERT INTO mm_fills (trade_id, market, tourn_date, ticker, side, tier, price_c, count, '
            'username, client_order_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (fill['trade_id'], market, tourn_date, fill['ticker'], fill['side'], fill['tier'],
             fill['price_c'], fill['count'], fill.get('username'), fill.get('client_order_id'),
             iso(utcnow())))
    return True


def list_fills(market: str, tourn_date: str | None = None, limit: int = 200) -> list[dict]:
    cols = ('trade_id', 'ticker', 'side', 'tier', 'price_c', 'count', 'username', 'ts')
    sql = f'SELECT {", ".join(cols)} FROM mm_fills WHERE market = ?'
    args: list[Any] = [market]
    if tourn_date:
        sql += ' AND tourn_date = ?'
        args.append(tourn_date)
    with get_conn() as conn:
        rows = conn.execute(sql + ' ORDER BY ts DESC LIMIT ?', (*args, limit)).fetchall()
    return [{**dict(zip(cols, r)), 'ts': _norm_ts(r[-1])} for r in rows]
