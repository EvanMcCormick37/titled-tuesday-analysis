#!/usr/bin/env python3
"""Apply the multi-market schema to the configured database.

Run this immediately before deploying the multi-market release. Safe to re-run.

Usage:
    python scripts/migrate_multi_market.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db import backend_name, get_conn
from src.multi_market_schema import migrate


def main() -> None:
    print(f'Migrating {backend_name()} database...')
    with get_conn() as conn:
        changed = migrate(conn)
    print(f'OK -- tables up to date: {", ".join(changed)}')


if __name__ == '__main__':
    main()
