# Attendance researcher (`src/research/`)

Finds other tournaments that may keep high-value players away from the next event. It is deterministic so far, with no LLM, and nothing it finds changes predictions: leads are advisory. A cross-market replacement is planned: `docs/proposals/conflict-registry.md`.

- Runs automatically as the last step of the weekly pipeline (`research`, right after `run_raw`, for the event `run_raw` predicted). A research failure is recorded in `research_runs` and the step summary but never fails the pipeline. It can also be re-run as the `research` ops job in the **`background` lane**: a subprocess on tt-worker (`scripts/research_attendance.py --market --date`) that never holds the market's trade lane, started from **Run now** on the Attendance page. The kill switch leaves it alone.
- **Candidates:** players whose raw-model fair value `p_participate × P_top8_given_play` is above 5% (`candidate_min_fv`, `candidate_n`), one row per player.
- **Calendar:** the Lichess broadcast API (`/api/broadcast/top` + `/api/broadcast`, every tier, then rounds and `/broadcast/{id}/players` for events within a day of the event), plus events you add on the Attendance page. chess.com/events can't be read automatically (Cloudflare challenges headless browsers; `scripts/chesscom_events_probe.py` showed it); add its events by hand. Don't add stealth or challenge-solving (why: `docs/decisions/2026-11-20-chesscom-events-not-scraped.md`).
- **Clash test** (`overlap.py`): window = start to start + `MarketSpec.event_duration_minutes` (TT 150, BB 120, FF 160). A round overlapping the window is `hard`, within `adjacent_hours` is `adjacent`, within `mid_event_hours` is `multi_day`. Round length depends on time control (classical 300 min). Season-long leagues count only through their actual rounds.
- **Name matching** (`names.py`): exact / strong / partial (surname + first initial, never a shared given name) / hint (the free-text players line).
- **Tables** (created by `job_queue.init_schema()`): `research_control` (params JSON), `research_runs` (candidates, leads, stats, collector errors), `research_events` (catalog, upserted by (source, source_id), with the operator's per-event `policy`, which re-collection never overwrites).
- **Migration:** the `background` lane needs `python scripts/migrate_research.py` once per database (it rebuilds the `trade_jobs.lane` CHECK; idempotent). Until then, queueing a research job fails.
- **Tests:** `tests/test_research.py`.

## LLM provider: minirouter.sh (tested, not yet used in production)

Facts from testing on 2026-11-20 that any LLM work here should rely on. `scripts/minirouter-docs.md` holds the provider docs, and `scripts/research_smoke_test.py` checks a model on the features below and prints the cost of each.

- OpenAI-compatible: `POST https://api.minirouter.sh/v1/chat/completions`, key in `MINIROUTER_KEY` (`.env.local` locally, tt-worker on Railway). Every response reports its charge in `usage.cost`.
- **Model: `zai/glm-5.3-flash`.** It classified a planted round clash correctly, kept to its search allowance, and supports every server tool. Backup: `openai/gpt-6-luna`. `anthropic/claude-haiku-5.5` under-classified the clash as `rumoured`. Test results: `docs/decisions/2026-11-20-llm-model-glm-flash.md`.
- **Never send `max_tool_calls` in a request that also offers our own functions.** Once the limit is reached, minirouter tells the model to finish without tools, which suppresses our function calls too. Cap searches with the search tool's `max_uses`, and enforce per-run budgets by summing `usage.server_tool_use`; models can exceed `max_uses`.
- **No automatic retries.** Every request is billed, even one whose response is lost. Retry once, only on a connection error before any response.
- Read pages through our own function backed by `POST /v1/tools/web-fetch`, not the server-side fetch tool: server-tool results never reach us, so we couldn't check what the model read. **Don't set `allowedModels` on the key**: keys with model restrictions can't use web-fetch. Set spend caps on the key instead.
- `minirouter:news_search` only works on Vercel-served models (GLM-5.3-Flash is one); others return 400.
- Search results are poor evidence on their own (unofficial calendars, minor local events). Use search to find pages, and base findings on a fetched page.

Design rules for that work, decided with the user: the LLM gathers facts and classifies them; code sets every number. Fetched pages are untrusted input, so an LLM never writes `manual_adjustments`, recomputes or trades on its own. The one action it might be allowed is requesting the kill switch, behind code gates (planned; `docs/proposals/conflict-registry.md` §11).
