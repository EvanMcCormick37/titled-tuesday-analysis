#!/usr/bin/env python3
"""Allow the `background` lane on trade_jobs (attendance research). Idempotent.

`trade_jobs.lane` has a CHECK constraint, and `CREATE TABLE IF NOT EXISTS` in
`job_queue.init_schema()` never changes an existing table, so databases created
before the researcher need this once. The research tables themselves are
created automatically at startup.

Run against Railway from tt-worker (`railway ssh`, then this command), or locally
with DATABASE_URL set for the one command. Deploy the code after migrating:
until then a research job couldn't be queued anyway.

    python scripts/migrate_research.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import job_queue as q          # noqa: E402
from src.db import get_conn, is_postgres  # noqa: E402

LANES_SQL = ', '.join(f"'{s}'" for s in q.LANES)


def _postgres() -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'trade_jobs'::regclass AND contype = 'c'"
        ).fetchall()
        lane_checks = [(name, d) for name, d in rows if 'lane' in d]
        if any("'background'" in d for _, d in lane_checks):
            return 'already allows background'
        for name, _ in lane_checks:
            conn.execute(f'ALTER TABLE trade_jobs DROP CONSTRAINT "{name}"')
        conn.execute(f'ALTER TABLE trade_jobs ADD CONSTRAINT trade_jobs_lane_check CHECK (lane IN ({LANES_SQL}))')
    return f'replaced {len(lane_checks)} lane check(s)'


def _sqlite() -> str:
    """SQLite can't alter a CHECK: rebuild the table (sqlite.org/lang_altertable.html, 'other changes')."""
    with get_conn() as conn:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='trade_jobs'").fetchone()
        if row is None:
            return 'no trade_jobs table (init_schema will create it with the new check)'
        old_sql = row[0]
        if "'background'" in old_sql:
            return 'already allows background'
        start = old_sql.index('CHECK (lane IN (')
        end = old_sql.index('))', start) + 2
        new_sql = old_sql[:start] + f'CHECK (lane IN ({LANES_SQL}))' + old_sql[end:]
        new_sql = new_sql.replace('trade_jobs', 'trade_jobs_new', 1)
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('PRAGMA legacy_alter_table=ON')   # rename must not rewrite trade_job_events' FK
        conn.execute(new_sql)
        conn.execute('INSERT INTO trade_jobs_new SELECT * FROM trade_jobs')
        conn.execute('DROP TABLE trade_jobs')
        conn.execute('ALTER TABLE trade_jobs_new RENAME TO trade_jobs')
        conn.execute('PRAGMA legacy_alter_table=OFF')
        conn.execute('PRAGMA foreign_keys=ON')
    q.init_schema()                                    # recreate the indexes dropped with the table
    return 'rebuilt trade_jobs with the new lane check'


def migrate() -> str:
    return _postgres() if is_postgres() else _sqlite()


if __name__ == '__main__':
    backend = 'Postgres' if is_postgres() else 'SQLite'
    print(f'{backend}: {migrate()}')
