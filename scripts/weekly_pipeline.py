#!/usr/bin/env python3
"""Weekly data pipeline — the single scheduled entry point.

Titled Tuesday runs the three pre-trade steps as one resumable state machine
(see src/pipeline_runner.py):

    scrape_tt → update_broadcasts → run_raw → ADJUSTMENTS_PENDING

Usage:
    python scripts/weekly_pipeline.py
    python scripts/weekly_pipeline.py --resume update_broadcasts
    python scripts/weekly_pipeline.py --slug-url https://chess.com/tournament/live/titled-tuesday-blitz-...
"""
import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

# Windows consoles default to cp1252; force stdout/stderr to UTF-8 so any
# non-ASCII character printed by downstream scraping scripts (player names,
# arrows in logging) doesn't crash the pipeline on an encoding error.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts' / 'scraping'))

import requests  # for RequestException classification

from src.markets import TITLED_TUESDAY
from src.pipeline import run_raw, next_tourn_date
from src.pipeline_runner import PipelineDef
from src.pipeline_runner import run_pipeline as run_market_pipeline
from update_titled_tuesday import (
    ListingMissingSlugError,
    _slug_from_url,
    discover_slugs,
    known_slugs,
    process as process_slug,
)
from update_broadcasts import run as run_update_broadcasts

STEPS = ('scrape_tt', 'update_broadcasts', 'run_raw')


def _classify_error(step: str, exc: BaseException) -> str:
    if isinstance(exc, ListingMissingSlugError):
        return 'listing_missing_slug'
    if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError, requests.RequestException)):
        return 'http_error'
    if step == 'run_raw':
        return 'computation_error'
    return 'unknown'


def step_scrape_tt(slug_override: str | None = None) -> dict:
    """Scrape new TT standings. Either resolves via listing or uses an explicit URL/slug.

    Raises ListingMissingSlugError if the listing has no new slug yet; the pipeline
    translates that into the specific error_kind the dashboard needs to show the
    URL input form.
    """
    if slug_override:
        slug = _slug_from_url(slug_override)
        print(f'  scrape_tt: slug override → {slug}')
        process_slug(slug)
        return {'mode': 'override', 'slugs': [slug]}

    seen = known_slugs()
    discovered = discover_slugs()
    new = [s for s in discovered if s not in seen]
    if not new:
        raise ListingMissingSlugError(
            'chess.com listing contains no TT slug not already in the DB '
            f'(latest discovered: {discovered[:3]})'
        )
    print(f'  scrape_tt: {len(new)} new tournament(s): {new}')
    for slug in new:
        process_slug(slug)
        time.sleep(2.0)
    return {'mode': 'listing', 'slugs': new}


def step_update_broadcasts() -> dict:
    return run_update_broadcasts(months=2)


def step_run_raw() -> dict:
    """Run the raw (unadjusted) MC simulation for the upcoming TT."""
    target = next_tourn_date()
    raw = run_raw(target, save_to_db=True)
    return {
        'tourn_date_predicted': target,
        'pool_size':            len(raw.players),
        'n_rows':               int(len(raw.results_raw)),
    }


def _run_tt_step(step: str, slug_override: str | None) -> dict:
    if step == 'scrape_tt':
        return step_scrape_tt(slug_override=slug_override)
    return {'update_broadcasts': step_update_broadcasts, 'run_raw': step_run_raw}[step]()


PIPELINES = {
    TITLED_TUESDAY.slug: PipelineDef(TITLED_TUESDAY, STEPS, _run_tt_step, _classify_error),
}


def run_pipeline(resume_from: str | None = None, slug_override: str | None = None,
                 market: str = TITLED_TUESDAY.slug) -> dict:
    return run_market_pipeline(PIPELINES[market], resume_from, slug_override)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--market', choices=sorted(PIPELINES), default=TITLED_TUESDAY.slug)
    ap.add_argument('--resume', help='Resume from this step instead of starting fresh.')
    ap.add_argument('--slug-url',
                    help='Direct chess.com tournament URL (or bare slug) to feed the scrape step. '
                         'Use this when the listing hasn\'t surfaced the event yet.')
    args = ap.parse_args()
    final = run_pipeline(args.resume, args.slug_url, args.market)
    print(json.dumps(final, indent=2, default=str))


if __name__ == '__main__':
    main()
