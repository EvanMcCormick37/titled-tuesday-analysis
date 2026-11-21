"""One research run (phase 1: deterministic, no LLM).

    candidates  →  collect (Lichess) into the catalog  →  catalog near the event day
                →  clash test + name match  →  leads, saved on the run row
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from ..markets import MarketSpec
from . import lichess, store
from .candidates import load_candidates
from .overlap import event_window, find_leads

COLLECTORS = ('lichess',)


def run(spec: MarketSpec, event_date: str, *, job_id: int | None = None,
        lichess_client: lichess.LichessClient | None = None, log: Callable[[str], None] = print) -> dict:
    params = store.params_for(spec.slug)
    run_id = store.start_run(spec.slug, event_date, job_id)
    log(f'Research run {run_id}: {spec.label} on {event_date}')
    try:
        candidates = load_candidates(spec, params)
        log(f'{len(candidates)} candidates (raw top-{params.candidate_n} fair value > {params.candidate_min_fv:.0%})')
        for c in candidates[:10]:
            log(f'  {c["player_name"]:28s} fair {c["fair_value"]:.1%}  plays {c["p_participate"]:.0%}')
        if len(candidates) > 10:
            log(f'  … and {len(candidates) - 10} more')

        stats: dict = {'candidates': len(candidates)}
        errors: dict[str, str] = {}
        try:
            events, stats['lichess'] = lichess.collect(date.fromisoformat(event_date), params,
                                                        lichess_client, log)
            for ev in events:
                store.upsert_event(ev)
        except Exception as e:  # noqa: BLE001 — one collector failing must not lose the others
            errors['lichess'] = f'{type(e).__name__}: {e}'[:300]
            log(f'! Lichess collector failed: {errors["lichess"]}')

        day = date.fromisoformat(event_date)
        catalog = store.events_overlapping((day - timedelta(days=params.calendar_days)).isoformat(),
                                           (day + timedelta(days=params.calendar_days)).isoformat())
        stats['catalog'] = len(catalog)
        stats['operator_events'] = sum(1 for e in catalog if e['source'] == 'operator')
        window = event_window(spec, event_date)
        leads = find_leads(candidates, catalog, window, params)
        stats['leads'] = len(leads)
        log(f'{len(catalog)} events near {event_date}; window {window.start:%H:%M}–{window.end:%H:%M} UTC; '
            f'{len(leads)} lead(s)')
        for lead in leads:
            who = lead['player_name'] or '(players not published yet)'
            log(f'  [{lead["kind"]:9s}] {who:28s} {lead["event_name"][:50]}'
                + (f'  match={lead["match"]}' if lead['match'] else ''))

        if errors and len(errors) == len(COLLECTORS):
            message = 'Every collector failed: ' + '; '.join(f'{k}: {v}' for k, v in errors.items())
            store.finish_run(run_id, 'failed', candidates=candidates, leads=leads, stats=stats,
                             collector_errors=errors, error=message)
            return {'run_id': run_id, 'status': 'failed', 'leads': leads}

        status = 'leads' if leads else 'no_leads'
        store.finish_run(run_id, status, candidates=candidates, leads=leads, stats=stats,
                         collector_errors=errors)
        return {'run_id': run_id, 'status': status, 'leads': leads}
    except Exception as e:
        store.finish_run(run_id, 'failed', error=f'{type(e).__name__}: {e}'[:500])
        raise
