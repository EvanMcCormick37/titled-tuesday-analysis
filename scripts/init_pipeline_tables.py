#!/usr/bin/env python3
"""Create the orchestration tables (manual_adjustments, pipeline_runs, job_runs).

Idempotent — safe to re-run. SQLite-flavoured. Phase 1 Postgres init lives in
scripts/init_postgres_schema.py and declares the same tables with SERIAL /
TIMESTAMPTZ / JSONB.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db import get_conn, is_postgres

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


def init(conn) -> None:
    for stmt in DDL:
        conn.execute(stmt)
    conn.commit()


def main() -> None:
    if is_postgres():
        raise RuntimeError(
            'This is the SQLite-only migration. For Postgres, run '
            'scripts/init_postgres_schema.py instead.'
        )
    with get_conn() as conn:
        init(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('manual_adjustments','pipeline_runs','job_runs') "
            "ORDER BY name"
        ).fetchall()
        print(f"OK -- orchestration tables present: {[t[0] for t in tables]}")


if __name__ == '__main__':
    main()
