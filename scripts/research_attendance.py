#!/usr/bin/env python3
"""Attendance research run: entry point of the `research` ops job (background lane).

Phase 1 is deterministic: candidates from the raw model, the Lichess broadcast
calendar plus operator-added events, and the time-clash test. Results go to
`research_runs` and `research_events`; the Attendance page shows them.

Usage:
    python scripts/research_attendance.py                          # Titled Tuesday, next event
    python scripts/research_attendance.py --market bullet_brawl
    python scripts/research_attendance.py --date 2026-11-24
"""
import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.markets import SPECS                      # noqa: E402
from src.pipeline import next_tourn_date           # noqa: E402
from src.research import store                     # noqa: E402
from src.research.run import run                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--market', default='titled_tuesday', choices=sorted(SPECS))
    ap.add_argument('--date', help='event date (YYYY-MM-DD); default: the next event')
    args = ap.parse_args()

    spec = SPECS[args.market]
    store.init_schema()
    job_id = os.environ.get('TT_JOB_ID')
    result = run(spec, args.date or next_tourn_date(spec), job_id=int(job_id) if job_id else None,
                 log=lambda m: print(m, flush=True))
    # Exit 0 even on a recorded failure: the ops job's verify step reads the run row.
    print(f'Result: {result["status"]}, {len(result["leads"])} lead(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
