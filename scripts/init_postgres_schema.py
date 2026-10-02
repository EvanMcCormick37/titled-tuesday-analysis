#!/usr/bin/env python3
"""Create the Postgres schema for Phase 1.

Mirrors the current SQLite shapes but with Postgres-native types:
  SERIAL / BIGSERIAL for autoincrement
  TIMESTAMPTZ with now() defaults
  JSONB for log summaries
  Explicit PRIMARY KEY / UNIQUE constraints

Mixed-case column names (P_topN_given_play, marketTicker, etc.) are quoted
so Postgres preserves case — matching exactly what pandas writes via to_sql
and what the raw-SQL callers expect.

Idempotent — safe to re-run (CREATE TABLE IF NOT EXISTS everywhere).
Requires $DATABASE_URL to be set; refuses to run against SQLite.

Usage:
    python scripts/init_postgres_schema.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db import get_conn, is_postgres


DDL = [
    # ── Raw TT data ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS titled_tuesday_standings (
        date                TIMESTAMPTZ,
        tournament_slug     TEXT,
        session             TEXT,
        rank                REAL,
        username            TEXT,
        title               TEXT,
        country             TEXT,
        rating              TEXT,
        score               REAL,
        tie_break           REAL,
        wins                REAL,
        draws               REAL,
        byes                REAL,
        aroc_1              REAL,
        performance_rating  REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tts_slug ON titled_tuesday_standings(tournament_slug)",
    "CREATE INDEX IF NOT EXISTS idx_tts_user ON titled_tuesday_standings(username)",
    "CREATE INDEX IF NOT EXISTS idx_tts_date ON titled_tuesday_standings(date)",

    """
    CREATE TABLE IF NOT EXISTS titled_tuesday_tournaments (
        date             TIMESTAMPTZ,
        time_local       TEXT,
        title            TEXT,
        session          TEXT,
        num_players      INTEGER,
        winner           TEXT,
        tournament_slug  TEXT UNIQUE,
        url              TEXT
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS player_information (
        username                TEXT PRIMARY KEY,
        player_name             TEXT,
        country                 TEXT,
        title                   TEXT,
        fide_rating             INTEGER,
        chess_com_blitz_rating  INTEGER,
        status                  TEXT,
        chess_com_blitz_best    INTEGER,
        profile_url             TEXT,
        fetch_error             TEXT,
        is_default              INTEGER DEFAULT 0
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS attendance_conflicts (
        date         TEXT NOT NULL,
        player_name  TEXT NOT NULL,
        username     TEXT NOT NULL,
        PRIMARY KEY (date, username)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attendance_conflicts_username ON attendance_conflicts(username)",

    # ── Broadcast events ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS other_events (
        broadcast_name      TEXT PRIMARY KEY,
        first_game_utc      TEXT,
        last_game_utc       TEXT,
        n_rounds            INTEGER,
        n_games             INTEGER,
        n_players           INTEGER,
        modal_time_control  TEXT,
        pct_titled          REAL,
        is_online           INTEGER,
        pct_with_fide_id    REAL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS other_event_rounds (
        broadcast_name       TEXT NOT NULL,
        round                TEXT NOT NULL,
        n_games              INTEGER,
        earliest_start_utc   TEXT,
        median_start_utc     TEXT,
        PRIMARY KEY (broadcast_name, round)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS other_event_participants (
        broadcast_name  TEXT NOT NULL,
        player_name     TEXT NOT NULL,
        PRIMARY KEY (broadcast_name, player_name)
    )
    """,

    # ── Predictions (mixed case preserved via quoting) ───────────────────────
    """
    CREATE TABLE IF NOT EXISTS latest_model_predictions (
        username                TEXT,
        p_participate           REAL,
        "P_top1_given_play"     REAL,
        "P_top3_given_play"     REAL,
        "P_top5_given_play"     REAL,
        "P_top8_given_play"     REAL,
        "P_top10_given_play"    REAL,
        tourn_date              TEXT,
        attendance_altered      INTEGER
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS latest_model_predictions_raw (
        username                TEXT,
        p_participate           REAL,
        "P_top1_given_play"     REAL,
        "P_top3_given_play"     REAL,
        "P_top5_given_play"     REAL,
        "P_top8_given_play"     REAL,
        "P_top10_given_play"    REAL,
        tourn_date              TEXT
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS historical_predictions (
        tourn_date              TEXT NOT NULL,
        username                TEXT NOT NULL,
        p_participate           REAL,
        "P_top1_given_play"     REAL,
        "P_top3_given_play"     REAL,
        "P_top5_given_play"     REAL,
        "P_top8_given_play"     REAL,
        "P_top10_given_play"    REAL,
        attendance_altered      INTEGER,
        PRIMARY KEY (tourn_date, username)
    )
    """,

    # ── Orchestration (Phase 0 tables) ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS manual_adjustments (
        id              BIGSERIAL PRIMARY KEY,
        tourn_date      TEXT NOT NULL,
        player_name     TEXT NOT NULL,
        adjustment_type TEXT NOT NULL
                        CHECK (adjustment_type IN ('cut','keep','override','nudge','global_nudge')),
        value           REAL,
        note            TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_adj_tourn ON manual_adjustments(tourn_date)",

    """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        id              BIGSERIAL PRIMARY KEY,
        tourn_date      TEXT NOT NULL UNIQUE,
        state           TEXT NOT NULL
                        CHECK (state IN ('running','adjustments_pending','completed','failed')),
        current_step    TEXT,
        failed_step     TEXT,
        error_kind      TEXT,
        error_message   TEXT,
        started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at    TIMESTAMPTZ
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS job_runs (
        id               BIGSERIAL PRIMARY KEY,
        pipeline_run_id  BIGINT REFERENCES pipeline_runs(id),
        job_name         TEXT NOT NULL,
        started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        ended_at         TIMESTAMPTZ,
        status           TEXT NOT NULL
                         CHECK (status IN ('running','success','failed')),
        summary          JSONB,
        log_tail         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_pipeline ON job_runs(pipeline_run_id)",

    # ── Backtest / historical analysis (notebook-only, but tables kept so
    #    the data migration round-trips cleanly) ──────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS backtest (
        tourn_date              TEXT NOT NULL,
        model                   TEXT NOT NULL,
        username                TEXT NOT NULL,
        p_participate           REAL,
        "P_top1_given_play"     REAL,
        "P_top3_given_play"     REAL,
        "P_top5_given_play"     REAL,
        "P_top8_given_play"     REAL,
        "P_top10_given_play"    REAL,
        played                  INTEGER,
        actual_rank             REAL,
        PRIMARY KEY (tourn_date, model, username)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS kalshi_markets (
        market_ticker     TEXT PRIMARY KEY,
        series_ticker     TEXT,
        event_ticker      TEXT,
        event_title       TEXT,
        tournament_date   TEXT,
        player            TEXT,
        status            TEXT,
        result            TEXT,
        settlement_value  REAL,
        open_time         TEXT,
        close_time        TEXT,
        lead_hours        REAL,
        volume            REAL,
        open_interest     REAL,
        tier              TEXT
    )
    """,

    # kalshi_market_snapshots: no PK — SQLite accepts NULL candle_time (markets
    # with no candle data yet), and the historical loader is content with
    # append-only semantics. Dedup happens via a UNIQUE index on the non-NULL
    # subset only.
    """
    CREATE TABLE IF NOT EXISTS kalshi_market_snapshots (
        market_ticker            TEXT,
        series_ticker            TEXT,
        event_ticker             TEXT,
        tournament_date          TEXT,
        player                   TEXT,
        open_time                TEXT,
        close_time               TEXT,
        lead_hours               REAL,
        target_time              TEXT,
        candle_time              TEXT,
        actual_hours_after_open  REAL,
        offset_error_h           REAL,
        yes_bid_close            REAL,
        yes_ask_close            REAL,
        mid                      REAL,
        spread                   REAL,
        last_trade_previous      REAL,
        cum_volume               REAL,
        open_interest            REAL,
        result                   TEXT,
        settlement_value         REAL,
        candles_in_window        INTEGER,
        tier_used                TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_kms_ticker_candle
    ON kalshi_market_snapshots(market_ticker, candle_time)
    WHERE candle_time IS NOT NULL
    """,

    """
    CREATE TABLE IF NOT EXISTS kalshi_portfolio (
        "marketTicker"      TEXT,
        "eventTitle"        TEXT,
        "marketTitle"       TEXT,
        "closeDate"         TEXT,
        volume              REAL,
        "yesPrice"          TEXT,
        "noPrice"           TEXT,
        "lastTradePrice"    TEXT,
        position            TEXT,
        "averagePrice"      TEXT,
        exposure            TEXT,
        "positionReturns"   TEXT,
        "lockedInReturns"   TEXT,
        "totalReturns"      TEXT
    )
    """,
]


def main() -> None:
    if not is_postgres():
        raise RuntimeError(
            'DATABASE_URL is not set to a Postgres URL. This script is Postgres-only; '
            'for SQLite use scripts/init_pipeline_tables.py.'
        )
    with get_conn() as conn:
        for stmt in DDL:
            conn.execute(stmt)
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        print(f'OK -- {len(rows)} tables in public schema:')
        for r in rows:
            print(f'  {r[0]}')


if __name__ == '__main__':
    main()
