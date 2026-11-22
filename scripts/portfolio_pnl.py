#!/usr/bin/env python3
"""Monte Carlo P&L of held positions: builds the Portfolio page's `pnl` snapshot.

tt-worker runs this as a subprocess so the simulation's CPU use can't stall
order sending. Positions (get_positions_df records) arrive as JSON on stdin;
the payload goes to stdout as JSON. Progress and warnings go to stderr.

Usage:
    python scripts/portfolio_pnl.py --market titled_tuesday --date 2026-11-24 < positions.json
"""
import argparse
import contextlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd                                   # noqa: E402

from src.markets import SPECS                         # noqa: E402
from src.portfolio import PNL_SIMS, simulate_positions_pnl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--market', default='titled_tuesday', choices=sorted(SPECS))
    ap.add_argument('--date', required=True, help='event date (YYYY-MM-DD)')
    ap.add_argument('--sims', type=int, default=PNL_SIMS)
    args = ap.parse_args()

    positions = pd.DataFrame(json.load(sys.stdin))
    with contextlib.redirect_stdout(sys.stderr):
        payload = simulate_positions_pnl(positions, SPECS[args.market], args.date, n_sims=args.sims)
    json.dump(payload, sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
