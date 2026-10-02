#!/usr/bin/env python3
"""On-demand `run_adjusted` entry point.

Reads manual_adjustments for the upcoming tournament, re-runs the adjusted MC
simulation, and writes `latest_model_predictions`. Logs a `job_runs` row keyed
on the matching `pipeline_runs` row and flips that row's state → 'completed'
on success.

Called by:
    - the Streamlit dashboard's "Save & recompute" button (via import),
    - the Telegram bot's /recompute command (via import),
    - this CLI, for local use.

Usage:
    python scripts/trigger_adjusted.py
    python scripts/trigger_adjusted.py --date 2026-10-14   # override the predicted TT date
"""
import argparse
import json
import sys
import traceback
from datetime import timedelta, datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db import get_conn
from src.pipeline import run_adjusted, load_adjustments, next_tourn_date


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')[:-4] + 'Z'


def _pipeline_run_id_for(conn, tourn_date_predicted: str) -> int | None:
    """Return the pipeline_runs.id whose scraped Tuesday is one week before the predicted TT."""
    scraped = (pd.Timestamp(tourn_date_predicted) - pd.Timedelta(weeks=1)).date().isoformat()
    row = conn.execute(
        'SELECT id FROM pipeline_runs WHERE tourn_date = ?',
        (scraped,),
    ).fetchone()
    return row[0] if row else None


def trigger_adjusted(tourn_date: str | None = None) -> dict:
    """Run run_adjusted() with DB-sourced adjustments; log + flip pipeline state."""
    target = tourn_date or next_tourn_date()
    print(f'[trigger_adjusted] target tourn_date = {target}')

    adj = load_adjustments(target)
    print(f'  loaded adjustments: '
          f'{len(adj["cut_players"])} cut, {len(adj["keep_players"])} keep, '
          f'{len(adj["p_participate_overrides"])} overrides, '
          f'{len(adj["p_nudges"])} nudges, global_nudge={adj["global_nudge"]:+.2f}')

    with get_conn() as conn:
        pr_id = _pipeline_run_id_for(conn, target)
        if pr_id is None:
            print(f'  [warning] no pipeline_runs row for scraped={target} - 7d; '
                  f'proceeding without pipeline_run_id link.')

        row = conn.execute(
            'INSERT INTO job_runs (pipeline_run_id, job_name, status) VALUES (?, ?, ?) RETURNING id',
            (pr_id, 'run_adjusted', 'running'),
        ).fetchone()
        job_id = row[0]

    try:
        adj_run = run_adjusted(target, save_official=True, **adj)
    except BaseException as exc:
        tb = traceback.format_exc()
        with get_conn() as conn:
            conn.execute(
                'UPDATE job_runs SET ended_at = ?, status = ?, summary = ?, log_tail = ? WHERE id = ?',
                (_now_iso(), 'failed', json.dumps({'error': str(exc)}), tb[-16_000:], job_id),
            )
        print(f'[trigger_adjusted] FAIL: {exc}')
        raise

    summary = {
        'tourn_date_predicted': target,
        'pool_size':            len(adj_run.players),
        'n_rows':               int(len(adj_run.results)),
        'cut_players':          len(adj["cut_players"]),
        'keep_players':         len(adj["keep_players"]),
        'overrides':            len(adj["p_participate_overrides"]),
        'nudges':               len(adj["p_nudges"]),
        'global_nudge':         adj["global_nudge"],
    }

    with get_conn() as conn:
        conn.execute(
            'UPDATE job_runs SET ended_at = ?, status = ?, summary = ? WHERE id = ?',
            (_now_iso(), 'success', json.dumps(summary), job_id),
        )
        if pr_id is not None:
            conn.execute(
                'UPDATE pipeline_runs SET state = ?, completed_at = ? WHERE id = ?',
                ('completed', _now_iso(), pr_id),
            )

    print(f'[trigger_adjusted] OK run_adjusted complete: {summary}')
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', help='Override the predicted TT date (default: one week after latest standings).')
    args = ap.parse_args()
    trigger_adjusted(args.date)


if __name__ == '__main__':
    main()
