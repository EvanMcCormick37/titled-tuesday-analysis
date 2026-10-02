#!/usr/bin/env python3
"""Create the orchestration tables (manual_adjustments, pipeline_runs, job_runs).

Idempotent — safe to re-run. SQLite flavour of the schemas defined in §4 of
ORCHESTRATION_AUTOMATION_PLAN.md. The Postgres cutover in Phase 1 will replace
INTEGER PRIMARY KEY AUTOINCREMENT with SERIAL and the ISO-string timestamp
columns with TIMESTAMPTZ, but the column names and semantics stay identical.
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH

# ISO-8601 UTC with ms precision — mirrors what Postgres' TIMESTAMPTZ serialises to.
_NOW_DEFAULT = "(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"


DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS manual_adjustments (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tourn_date      TEXT NOT NULL,
        player_name     TEXT NOT NULL,
        adjustment_type TEXT NOT NULL
                        CHECK (adjustment_type IN ('cut','keep','override','nudge','global_nudge')),
        value           REAL,
        note            TEXT,
        created_at      TEXT NOT NULL DEFAULT {_NOW_DEFAULT},
        created_by      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_adj_tourn ON manual_adjustments(tourn_date)",

    f"""
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tourn_date      TEXT NOT NULL UNIQUE,
        state           TEXT NOT NULL
                        CHECK (state IN ('running','adjustments_pending','completed','failed')),
        current_step    TEXT,
        failed_step     TEXT,
        error_kind      TEXT,
        error_message   TEXT,
        started_at      TEXT NOT NULL DEFAULT {_NOW_DEFAULT},
        completed_at    TEXT
    )
    """,

    f"""
    CREATE TABLE IF NOT EXISTS job_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_run_id  INTEGER REFERENCES pipeline_runs(id),
        job_name         TEXT NOT NULL,
        started_at       TEXT NOT NULL DEFAULT {_NOW_DEFAULT},
        ended_at         TEXT,
        status           TEXT NOT NULL
                         CHECK (status IN ('running','success','failed')),
        summary          TEXT,
        log_tail         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_pipeline ON job_runs(pipeline_run_id)",
]


def init(conn: sqlite3.Connection) -> None:
    for stmt in DDL:
        conn.execute(stmt)
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        init(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('manual_adjustments','pipeline_runs','job_runs') "
            "ORDER BY name"
        ).fetchall()
        print(f"OK — orchestration tables present: {[t[0] for t in tables]}")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
