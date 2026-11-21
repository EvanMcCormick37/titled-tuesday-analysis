Architecture proposal · Kalshi automated trading

# LLM attendance researcher

20 November 2026 · Draft v2 for review

**Changes since v1:** chess.com/events can't be read automatically (Cloudflare challenges headless browsers, tested), so the Lichess broadcast API is the primary automated calendar and chess.com events are pasted in from the dashboard (§3, stage 2). The researcher can trigger an **emergency halt**, i.e. the kill switch, behind code gates (new §7). The LLM layer is now specific to minirouter.sh, with a recommended model and a check script (§8).

**Contents**

1. [Summary](#1-summary)
2. [Where it fits in the existing system](#2-where-it-fits-in-the-existing-system)
3. [The research pipeline](#3-the-research-pipeline)
4. [Proposed vs working adjustments](#4-proposed-vs-working-adjustments)
5. [Operator context and the event catalog](#5-operator-context-and-the-event-catalog)
6. [Scheduling](#6-scheduling)
7. [Emergency halt](#7-emergency-halt)
8. [The LLM layer (minirouter.sh)](#8-the-llm-layer-minirouter-sh)
9. [Dashboard](#9-dashboard)
10. [Data model](#10-data-model)
11. [Safety, cost and failure modes](#11-safety-cost-and-failure-modes)
12. [Evaluation](#12-evaluation)
13. [Phased roadmap](#13-phased-roadmap)
14. [Open questions](#14-open-questions)

## 1. Summary

Attendance is the biggest single input to fair value for a top player: when `p_participate` is wrong, the market maker's anchors are wrong. Today a human finds conflicts by hand and types them into the Attendance page. This proposal adds a **researcher**: a queued job that runs every few hours. It collects the upcoming chess calendar, works out which high-impact players might be busy during the event window, investigates them, and writes a **proposal**: a set of cut / keep / override / nudge items with reasons and sources. The proposal waits on the Attendance page until you accept, edit or reject each item. Only accepted items reach `manual_adjustments`, the working set `run_adjusted` reads.

Main design decisions:

| Decision | Why |
|---|---|
| **The LLM gathers facts; code sets the numbers.** The model sorts each player into an evidence category (e.g. `hard_round_conflict`, `confirmed_absent`) with a confidence and sources. A deterministic policy table, which you control, maps categories to adjustments, using the player's own historical attendance on conflict weeks where we have it. | Cheap models are decent at reading pages and poor at calibration. Numbers produced by code are reproducible, testable and tunable without prompt engineering. |
| **Most of the work involves no LLM.** Candidate players, the event list, round times and time-window overlaps are computed in Python before the model is called. If nothing changed since the last run, the model isn't called at all. | Keeps an inexpensive model on a short, focused task and keeps cost close to zero on quiet runs. |
| **Proposals are a separate layer from the working set.** Each item is accepted, edited or rejected on its own. Rejections are remembered and passed back to the model, so it doesn't propose the same thing every three hours. | This is what you asked for: rejecting is one tap, and a re-run never overwrites your working adjustments. |
| **Standing operator context.** Notes with an expiry date, plus a per-event policy (`ignore`, `nudge only`, `cut`, `let the researcher decide`) on a persistent catalog of detected tournaments. | You write the team-tournament rule once, not every run, and it's enforced in code as well as in the prompt. |
| **Runs in the trade queue on a new `background` lane.** | You get the job drawer, live log, cancel, and the Logs page for free, and research never holds the market's trade lane (§2.2). |
| **Read-only, with one exception: the emergency halt.** The researcher can't write `manual_adjustments`, recompute, or place orders. It *can* trigger the existing kill switch when the model asks for it **and** code-checked conditions pass (§7). | Fetched web pages are untrusted input. A bad proposal gets rejected. A false halt cancels orders, which costs some missed edge and never creates a position. So the one action the researcher can take is the one whose downside is bounded. |

**Placement (decided):** a **Proposals** section at the top of the existing Attendance page, plus a **Context** sub-view on the same page, rather than a new top-level tab. Everything a proposal does is edit the attendance working set, so review and editing belong side by side. Run history opens in the existing job drawer.

## 2. Where it fits in the existing system

```
 Lichess broadcast API (primary)     ┐  ┌──────────────────────────────┐
 FIDE / chess-results                ├─▶│ tt-worker                    │
 chess.com events (pasted by you)    ┘  │  background lane:            │
                                        │   research job (subprocess)  │
                                        │     ├ deterministic dossier  │
                                        │     ├ LLM investigation      │◀─▶ minirouter.sh
 Postgres ─────────────────────────────▶│     │   (+ halt gate, §7) ───┼──▶ enqueue_kill (priority lane)
  latest_model_predictions_raw          │     └ policy → proposal      │     pauses MM, cancels orders
  attendance_conflicts, standings       └──────────────┬───────────────┘   (chat, Google search,
  research_context, research_events                    │ writes              web fetch)
                                                       ▼
                                         adjustment_proposals (+ items)
                                                  │  you: accept / edit / reject
                                                  ▼
                                         manual_adjustments  (working set, unchanged)
                                                  │  "Save and recompute" (existing ops job)
                                                  ▼
                                         latest_model_predictions → tt-mm reprices
```

### 2.1 What stays the same

- `manual_adjustments`, `load_adjustments()`, `run_adjusted()` and `trigger_adjusted.py` are unchanged. The working set stays the only input to the adjusted model. Researcher-sourced rows are marked in the existing `created_by` column (`researcher:<proposal_id>`) and carry their rationale in the existing, so far unused, `note` column.
- Recompute stays an ops job in the market's normal lane, so the MM and trade jobs keep their guarantee that predictions never change mid-plan.
- No new Railway service. tt-worker runs the job; tt-api serves the UI.

### 2.2 Why a new queue lane

Ops jobs (`recompute`, `run_pipeline`) share the market's single normal lane on purpose: a trade must never plan against predictions that are mid-recompute (`src/job_queue.py`). A research run is different. It reads predictions but never writes them, and it can take 5–10 minutes of network and LLM time. In the normal lane it would block `take_trades` and the kill switch's follow-up jobs for the whole run.

So the proposal adds a third lane, `background`, for jobs that never mutate predictions or orders:

- `trade_jobs.lane` CHECK becomes `('normal','priority','background')`. This is a schema change and needs a migration (`scripts/migrate_research.py`, idempotent; it rebuilds the CHECK on Postgres and recreates the table on SQLite). Unlike the new research tables, it can't happen in `init_schema()`, because `CREATE TABLE IF NOT EXISTS` doesn't alter an existing constraint.
- `claim_next()` changes its lane test from `j.lane='priority' OR NOT EXISTS(...)` to `j.lane <> 'normal' OR NOT EXISTS(...)`. The partial unique index already covers only `lane='normal'`, so nothing else changes.
- `Worker.tick()` claims from `('priority', 'normal', 'background')`.
- `OpsJob` gains `lane: str = 'normal'`, and `submit_ops()` passes it through. The research job registers as `OpsJob('research', 'Research attendance', ..., lane='background', timeout_s=15*60)`.
- The kill switch ignores background jobs, as it already ignores ops jobs.
- `has_active_job(market, 'research')` already prevents duplicates.

It runs as a subprocess (`scripts/research_attendance.py --market --date`) like the other ops jobs, so a stuck HTTP call or memory blow-up can't stall the worker, and stdout streams into the job log. That means the drawer shows the agent's tool calls live.

### 2.3 What date it researches

The job targets `market.next_event_date()`, the same date the Attendance page edits. It is only scheduled once the prior event's pipeline has reached `adjustments_pending` or `completed`. Between an event and its weekly scrape, `next_event_date()` still points at the event that just happened, so research then would be wasted.

## 3. The research pipeline

One run has five stages. Only stage 3 calls the LLM.

### Stage 1: candidates (DB only)

The players worth researching are those whose attendance moves money:

- **Rule (decided):** every player whose **raw-model fair value for the top-8 contract**, `p_participate × P_top8_given_play` from `latest_model_predictions_raw`, is **above 5%**, plus anyone with an open position or resting order on the event. Using the raw model means a cut never hides a player from later runs. The threshold and N live in `research_control.params` (`candidate_min_fv` = 0.05, `candidate_n` = 8) and can be set per market.
- Because the rule multiplies by `p_participate`, strong players who rarely play (e.g. Sindarov) fall out: a clash barely changes the fair value of a contract on someone who wasn't likely to show up anyway. Players whose *playing* odds are high but whose skill puts them just under the line also fall out, which is fine for the same reason: their contracts are cheap.
- For each candidate, a profile: chess.com accounts, display name, FIDE name and federation (`player_information`), current `p_participate`, attendance over the last 12 events, current working adjustment, and **historical attendance on conflict weeks** (`_compute_conflict_rates` in `src/attendance.py` already computes this from `attendance_conflicts` and the broadcast tables; it moves into a shared helper).

### Stage 2: collectors and the overlap test (no LLM)

Each collector returns normalised `EventRecord`s: name, source URL, start/end dates, round schedule with time zones where known, and participant list where known. Collectors are independent and cached by content hash.

| Source | What it gives | Notes |
|---|---|---|
| **Lichess broadcast API** (primary, automated) | Official JSON (`/api/broadcast/top`, then `/api/broadcast/{tourId}` for each event's rounds), with exact round `startsAt` times, a tier for event importance, the time zone, and often the player list | Built for programmatic access, free, no browser. Same source family as `update_broadcasts.py`, but forward-looking (the PGN dumps are monthly and retrospective). |
| **chess.com/events** (operator-supplied) | The most current list of major events | Can't be read automatically (below). You paste an event link or the page text on the dashboard. |
| **chess-results.com** | Per-player OTB registrations | `scripts/scraping/chess_conflicts.py` already does a player search; wrap it as a collector, limited to the top ~15 candidates to stay polite. |
| **FIDE calendar** | Rated events by date | Coverage backstop. |
| Agent web search (stage 3) | News, announcements, player posts; Google's index also covers chess.com event pages, so their titles and snippets turn up in searches | Only used for open questions the collectors couldn't settle. |

**All tiers are collected (decided).** A top player can enter a minor open, so nothing is filtered by Lichess tier or event size; the candidate match decides relevance. To keep that affordable: from `/api/broadcast/top` (paged), only events whose date range includes the event day are followed to `/api/broadcast/{tourId}` for their rounds. Minor events often have no `players` field, so for any event with a round on the event day and no player list, the collector reads the PGN headers of its latest round (`White`, `Black`, `WhiteFideId`, `BlackFideId`) and matches them against the candidates' FIDE names and IDs. Responses are cached and the whole collector runs at most every 2 hours, so it costs tens of requests to a public API, not hundreds.

**Lichess coverage, checked 2026-11-20:** `/api/broadcast/top` returned 56 active events, from the World Championship and the Total Chess finals down to opens, with round times. For example, the Spanish Club Team Championship (Nihal, Maghsoodloo, Yakubboev) plays a round at 15:00 UTC daily through 26 November, so on Tuesday the 24th a classical round starts an hour before Titled Tuesday and is still in progress when it begins. That's a lead the overlap test would produce with no LLM involved.

#### Why chess.com/events isn't scraped

Tested 2026-11-20. The page is an HTML shell; the event list is drawn by a client-side script, and plain HTTP requests for that script get a Cloudflare challenge. A headless Chromium probe (`scripts/chesscom_events_probe.py`) from a residential connection got **HTTP 403 `cf-mitigated: challenge` on the page itself**. A Railway datacenter IP would be treated at least as strictly, so the Railway test was skipped. Reading it automatically would mean disguising the automation (stealth plugins, spoofed browser identity, challenge solvers), which this design rules out. It's also fragile: it would break whenever Cloudflare changes its rules.

So chess.com/events becomes an operator input rather than a collector:

- **"Add events" box in the Context section.** Paste a chess.com event URL, or select-all and copy the text of chess.com/events (or any event page) from your own browser. One small LLM extraction call turns it into `EventRecord`s (name, dates, rounds, time zone, players). They're saved to `research_events` with `source='operator'` and the paste time, and from then on are treated like collected events, including the overlap test. Checking the page you already read once or twice a week becomes one paste.
- **`fetch_page` refuses `www.chess.com` URLs** and returns a short "not readable; use operator-supplied data or search snippets" message, so the agent doesn't waste calls on challenge pages. (`api.chess.com`, which the existing scrapers use, isn't affected.)
- **The proposal shows how old the operator data is** ("chess.com events last pasted 3 days ago"), so it's obvious when it needs refreshing.
- No browser in the Docker image.

If chess.com ever offers sanctioned access (an API key or a feed), the collector slot is ready for it.

Then the **overlap test**: each `MarketSpec` gains a `research` config with the event window (Titled Tuesday: start to roughly start + 2h30; Bullet Brawl: the 2-hour arena), plus travel and rest padding. For every event with a round inside the window, or a round within the padding on the same day, plus every event a candidate is known to be in, we produce a `ConflictLead(event, player?, round_time, overlap_kind)`, where `overlap_kind` is `hard` (round during the window), `adjacent` (same day, outside the window), or `multi_day` (the player is mid-event; travel or rest risk).

Leads are matched to the persistent event catalog (§5.2), so the operator's per-event policy is already attached before the model sees anything.

**Delta check:** the run hashes (candidate set, working adjustments, leads, collector content, operator context). If the hash equals the last successful run's hash, it stops here, records `skipped_unchanged`, and the existing proposal stays as is. On a quiet weekday this is most runs.

### Stage 3: investigation (LLM, bounded)

The model gets a short, structured dossier, not raw pages: the event window, the candidates with their profiles, the leads, the catalog policies, your standing context, and rejection history. Its job is to settle each lead: is the player actually in this event, does the round really clash once time zones are applied, is there an announcement either way.

Rather than one long free-roaming loop, which cheap models handle badly, it runs as **map then reduce**:

- **Map:** one small agent loop per event that has leads (often 0–4 events). Tools: minirouter's built-in `minirouter:google_search` (and `news_search` if the chosen model supports it), plus our own functions `fetch_page(url)`, `get_player(name)` and `get_event(id)` (§8.2). Budget: about 8 tool calls and a fixed token cap per event. Output: per-player findings, each with an `urgent` flag and reason if the model thinks it justifies a halt. **The halt gate (§7) is checked after each map loop**, not at the end of the run, so a breaking finding acts within a minute or two.
- **Reduce:** one call that sees every finding, the operator context and the rejection history, removes duplicates, settles contradictions, and calls `submit_findings` once.

`submit_findings` is a forced tool call validated against a strict schema (pydantic, `extra='forbid'`):

```json
{
  "findings": [
    {
      "player": "Hikaru Nakamura",
      "event_id": 412,
      "category": "hard_round_conflict",
      "confidence": 0.85,
      "rationale": "Round 6 of the Bundesliga starts 17:00 CET (11:00 ET), the same moment Titled Tuesday starts; he's on the published lineup.",
      "sources": ["https://…", "https://…"]
    }
  ],
  "no_change": ["Magnus Carlsen"],
  "notes": "Freestyle event in Paris ends Monday; no conflict found.",
  "halt_requested": false,
  "halt_reason": null
}
```

Categories (a fixed enum):

| Category | Meaning |
|---|---|
| `confirmed_absent` | The player or the organiser said they won't play, or they're on a flight etc. with a source |
| `hard_round_conflict` | A confirmed participant with a round that overlaps the event window |
| `soft_conflict` | Same day, adjacent to the window, mid-event travel or rest day; or a team event where sitting out is plausible |
| `rumoured` | Unconfirmed reports |
| `confirmed_playing` | A positive signal: announced stream, returning from a break |
| `conflict_resolved` | A previous conflict no longer applies (withdrew, event rescheduled) |

The model never outputs probabilities or log-odds.

### Stage 4: policy (code)

`src/research/policy.py` turns findings into proposal items. It's a pure function, unit-tested like `quoting.quote_side`:

1. **Event policy first.** If the catalog says `ignore`, drop the finding. If it says `nudge_only` with a value, the item is that nudge whatever the category. If it says `cut`, propose a cut. Your instruction is enforced here, not just requested in the prompt.
2. **Category defaults** (configurable per market, stored like `mm_control.params`):

| Category | Default adjustment |
|---|---|
| `confirmed_absent` (confidence ≥ 0.7) | `cut` |
| `hard_round_conflict` | `override` = the player's historical attendance rate on conflict weeks if they have ≥ 3 such weeks, else `cut` |
| `soft_conflict` | `nudge −0.7` |
| `rumoured` | `nudge −0.3`, or nothing if confidence < 0.5 |
| `confirmed_playing` | `override 0.95` (not `keep`, unless the source is the player confirming) |
| `conflict_resolved` | `clear` (remove the existing working adjustment) |

3. **Confidence scaling** for nudges: the default value times confidence, rounded to 0.1.
4. **Bounds:** overrides in [0, 1], nudges in [−3, +3]. Anything that fails is dropped and logged.
5. **Diff against the working set.** Items identical to what's already in `manual_adjustments` are marked `already_applied` and collapsed. Items matching a previous rejection (same player, type, value within tolerance, no new source URLs) are marked `previously_rejected` and collapsed but stay visible.

This doesn't double-count with the base model: `load_and_prepare()` already removes conflict weeks from the participation denominator, so `p_participate` is a conflict-free-week rate, and a conflict-week adjustment on top of it is the correct correction.

### Stage 5: write the proposal

A new `adjustment_proposals` row with its items. Any earlier `pending` proposal for the same (market, date) becomes `superseded`. Its undecided items carry over into the new one, so you never lose an item you haven't looked at yet. Decided items stay in history. The research run row records cost, tokens, tool-call count, source hashes and the full transcript.

## 4. Proposed vs working adjustments

```
 research run ──▶ proposal (pending)
                    ├ item: cut Nakamura         [accept] [edit] [reject]
                    ├ item: nudge −0.7 Firouzja  [accept] [edit] [reject]
                    └ item: clear Caruana        [accept] [edit] [reject]
                         │
          accept / edit  │  reject (+ optional reason)
                         ▼
 manual_adjustments (working set)          adjustment_rejections (memory)
```

- **Per-item decisions**, plus **Accept all** and **Reject all**. **Edit** lets you change type and value before accepting (e.g. turn the proposed cut into a −0.5 nudge). The edited value is what's applied, and the original is kept for evaluation.
- **Applying merges, it doesn't replace.** Today's `PUT /adjustments` deletes and rewrites the whole set. Accepting instead replaces only that player's rows (or deletes them for `clear`), following the same last-write-wins rules as `load_adjustments()`. Implemented as `POST /api/markets/{slug}/proposals/{id}/decide` with `{items: [{id, decision, type?, value?, reason?}], recompute: bool}`.
- **Optimistic concurrency.** `GET /adjustments` returns a `working_version` (a counter in a small `adjustment_versions` table, bumped on every write). `PUT` and `decide` must send it and get a 409 if the working set changed underneath. This prevents an "Accept" on your phone from being silently erased by an older form you then save on the laptop.
- **A rejection is remembered** in `adjustment_rejections` (player, type, value, source URLs, reason, date), scoped to the event date. It feeds the policy diff (§3, stage 4) and the next run's prompt ("the operator rejected cutting X because: …"). A rejection with a reason works as event-scoped operator context.
- **Recompute is still your call.** The decide endpoint takes `recompute: true`, which queues the existing `recompute` ops job, just like "Save and recompute".

## 5. Operator context and the event catalog

### 5.1 Standing context notes

`research_context` rows: free text, market (or all markets), optional player tags, optional event link, `active_until` date. Example:

> **Until 2026-12-15, all markets.** The European Club Cup team rounds clash with Titled Tuesday, but top TT players usually sit out that round or make a quick draw so they can play. Treat it as a negative nudge (about −0.5), never a cut.

Active notes go into the system prompt under a heading that says operator guidance overrides the researcher's own inference. Notes expire on their own, so stale guidance doesn't build up.

There's also a **one-off context box** on "Run now", stored with that run only.

### 5.2 Event catalog with policies

Every event a collector finds is upserted into `research_events` (name, source URLs, dates, round schedule, participants seen). It persists across runs, and each event can carry an **operator policy**:

| Policy | Effect in stage 4 |
|---|---|
| `auto` (default) | Category defaults apply |
| `ignore` | Findings for this event are dropped |
| `nudge_only` (value) | Every affected player gets exactly this nudge |
| `cut` | Every confirmed participant with a hard conflict is cut |
| `override` (value) | Every affected player gets this p_participate |

This is the structured form of your team-tournament example: set the event to `nudge_only −0.5` once and every run for every affected player follows it. The free-text note explains *why* to the model, and the policy *guarantees* the outcome. Policies optionally apply only to specific players ("Nakamura always plays TT regardless").

## 6. Scheduling

tt-worker already runs periodic duties (snapshots, the MM watchdog). It gains a third, `_maybe_schedule_research()`, checked about once a minute:

- For each market with `research.enabled`, enqueue `research` if no research job is active and the last run started at least `interval` ago, using DB timestamps so a restart doesn't cause a burst.
- **The interval tightens as the event nears** (defaults, editable): every 6 h until 48 h before, every 3 h until 12 h before, every 1 h after that. A **final run** at cutoff − 90 min (Titled Tuesday: 7:30 ET), so there's time to review before the MM's 9:00 cutoff.
- **Window:** from the prior event's pipeline reaching `adjustments_pending`, to the MM cutoff. Nothing outside it.
- A pause toggle and the interval settings live in `research_control` (same pattern as `mm_control`), changeable from the dashboard.

I recommend this over a Railway cron service. The schedule depends on the event date and the pipeline state, which a fixed cron can't express. It's editable from the dashboard, and it adds no service to deploy. Because of the delta check (§3), frequent runs are cheap: an unchanged run costs a few HTTP fetches and no tokens.

### Alerts for urgent findings

A new pending item that would move a top-10 candidate's fair value by more than X¢ (computed from `P_top{N}_given_play × Δp`) raises an alert in the dashboard banner, reusing the `mm_alerts` pattern, and optionally a push notification. Findings above the halt threshold go further (§7).

## 7. Emergency halt

Late news of a star's absence is exactly when your resting YES anchors on that player get picked off, and when the NO side and every other player's prices are wrong too, because the absent player's share of the win probability moves to everyone else. So the researcher can trigger the existing kill switch.

### 7.1 What it does

It calls `job_queue.enqueue_kill(market, tourn_date, created_by='researcher:<run_id>')`, the same function as the dashboard button. In one transaction that pauses the market maker for the market, cancels queued trade jobs, flags running ones to stop, and queues the priority-lane `cancel_bids` sweep, which waits for tt-mm to confirm the pause and sweeps twice if needed. The research subprocess needs only DB access, not Kalshi credentials. The halt is per market: a Titled Tuesday finding doesn't halt Bullet Brawl.

### 7.2 Conditions (all must hold)

The model can only *request* a halt (`halt_requested` / per-finding `urgent`). Code decides:

1. **The finding has a hard category:** `confirmed_absent` or `hard_round_conflict`, with confidence ≥ `halt_min_confidence` (default 0.8).
2. **It's grounded:** the cited source was fetched through our own `fetch_page` in this run, and its text contains the player's surname and the event name (§8.3). A search snippet alone isn't enough.
3. **It's new:** the player has no working adjustment that already reflects it (no cut or override ≤ 0.2), and the event's operator policy isn't `ignore` or `nudge_only`. An operator rule always wins.
4. **It's material:** the approximate fair-value move, `Δp × P_top{N}_given_play` from current working p to the policy-mapped p, is **at least the market maker's buffer** (`mm.store.params_for(market).buffer_c`, default 2¢) on at least one traded contract, **and** we have exposure to it: resting orders or MM quotes on that player, or open positions on the event above `halt_min_exposure` (default $50). Without exposure there's nothing to protect.

   **Why the buffer (decided):** the scout never bids above `fair − buffer_c`, so the buffer is the cushion between our highest resting bid and fair value. A fair-value move smaller than the buffer leaves every resting bid below the new fair value, still positive expected value, so the next recompute can fix it calmly. A move of at least the buffer can put our scouts *above* the new fair value, where every fill loses money. That's when trading should stop. There is one threshold, not two: change the buffer on the Market maker page and the halt follows it, including per-market overrides. The dashboard shows it as "Halt threshold: follows MM buffer (2¢)".

   The move is computed on that player's own contracts only, a first-order estimate. The absent player's win share goes to everyone else, so other players' contracts move too, but the halt is market-wide, so those orders are cancelled anyway.
5. **It's in time:** before the event start, while orders can be resting.
6. **It hasn't fired before:** at most one halt per (market, event date, player, event). After you resume, the same finding can't halt again. A different player or a different event still can.

If the model requests a halt and a gate fails, the run records `halt_declined` with the failing gate. That's visible on the run and useful for tuning.

### 7.3 Modes

`research_control.params.halt_mode`, following the MM's shadow-then-live pattern:

| Mode | Behaviour |
|---|---|
| `off` | Never halts; urgent findings only alert |
| `shadow` (default at launch) | Evaluates the gates and records "would have halted" with an alert, but doesn't call the kill switch |
| `live` | Halts |

Run in `shadow` for a few events, check every "would have halted" against what actually happened, then switch to `live`.

### 7.4 After a halt

- The banner shows **"Trading halted by researcher"** with the player, event, sources and the run link. A push notification goes out if configured.
- The finding sits at the top of the pending proposal, already marked urgent.
- **Accept** applies the adjustment and queues a recompute. The MM stays paused until you press Resume, because you'll want to check the repriced quote sheet first.
- **Reject as false alarm** records the rejection and offers **Resume market maker** in the same dialog.
- Nothing ever resumes trading automatically.

A narrower option for later: a per-player halt that pulls only that player's quotes (`mm.store.pull_player`) rather than the whole market. It's cheaper when the news is about one mid-ranked player. It's left out of v2 because a star's absence moves every contract on the event.

## 8. The LLM layer (minirouter.sh)

minirouter is OpenAI-compatible: `POST https://api.minirouter.sh/v1/chat/completions` with `Authorization: Bearer mr-live-…`. It accepts our own `function` tools and `tool_choice: "required"`, reports the charge in `usage.cost` on every non-streaming response, and offers built-in server tools that run inside a single request.

### 8.1 Adapter

`src/research/llm.py`, about 100 lines over `requests`:

```python
class ChatModel(Protocol):
    def complete(self, messages: list[dict], tools: list[dict] | None,
                 tool_choice: str | None, max_tokens: int, **extra) -> Completion: ...
    # Completion = message, tool_calls, usage (tokens, server_tool_use), cost_usd, provider
```

- Non-streaming requests with a 180 s timeout. Each request is short because our own function calls return to us between turns.
- **No automatic retries.** minirouter's docs say every request is billed separately and a lost response may still cost money. One retry, only on a connection error before any response arrives.
- Sums `usage.cost` per run for the budget, and records `x-minirouter-provider` and `x-minirouter-balance-usd` on the run. A low balance raises an alert before runs start failing.
- Uses the lowest `reasoning.effort` the model offers for map loops, and a higher one for reduce if wanted.

### 8.2 Which tool runs where

| Need | Implementation | Cost | Why |
|---|---|---|---|
| Search | Server tool `minirouter:google_search` (`max_uses` 2 per request, `max_results` 5) | $0.01 per search | Works on any paid tool-capable model. The model only needs titles, URLs and snippets to decide what to read. |
| News | Server tool `minirouter:news_search`, recency `week`, if the chosen model supports it | ~$0.012 per search | Better than Google for "withdrew" or "replaced" announcements. Only works on Vercel-served models; others get a 400. |
| Read a page | **Our own function** `fetch_page(url)`, backed by minirouter's standalone `POST /v1/tools/web-fetch` endpoint | $0.001 per read | Server-tool results stay inside minirouter and never reach us, so we couldn't check grounding. Going through our function means we log the exact text the model saw. The endpoint also blocks private-network addresses for us. Send a fresh `Idempotency-Key` per read. |
| Event details | Our function `get_event(id)`, returning what's in `research_events` (Lichess rounds, pasted chess.com events) | free | chess.com pages can't be fetched; `fetch_page` refuses them. |
| DB facts | Our functions `get_player(name)` and `get_event(id)` | free | Profiles, attendance history, past conflict-week attendance. |
| Final output | Our function `submit_findings`, the only tool offered on the last turn, with `tool_choice: "required"` | tokens | Validated with pydantic, one repair retry. |

Server tools and our functions can go in one request: minirouter runs the searches itself and returns our function calls to us.

**Never send `max_tool_calls` in a request that also offers our functions.** Tested 2026-11-20 on GLM-5.3-Flash: with `max_tool_calls` set, minirouter tells the model to finish without tools once the limit is reached, and that suppresses our function calls too. The model wrote "I'll record it now" and returned no call. Without it, the function call comes back with `tool_choice` either `auto` or `required`. Cap searches per request with the search tool's `max_uses`, and enforce the per-run budget by summing `usage.server_tool_use` across requests. Models can exceed `max_uses` (GPT-6 Luna spent about 2× its news allowance in testing), so the per-run cap is checked after every request.

### 8.3 Making a cheap model reliable

- **Small, typed tasks:** per-event map loops with structured inputs; the model classifies rather than estimates.
- **Forced structured output** as above.
- **Trimmed tool output:** `fetch_page` asks for `max_content_tokens` 6000 and returns title, text, `retrieved_at` and `truncated`. Cached by URL for the run.
- **Grounding check in code:** every finding needs at least one source URL read through `fetch_page` in this run. For `confirmed_absent` and `hard_round_conflict`, that page's text must contain the player's surname. Findings that fail are downgraded to `rumoured`, which also makes them ineligible to halt.
- **Fixed prompts in the repo** (`src/research/prompts/`), versioned, with the version stored on each run.

### 8.4 Model choice

The research loop needs four things: reliable function calling with `tool_choice: "required"`, mixing server tools with our functions, a long context for fetched pages, and a low price. From minirouter's public catalog (20 November 2026, $ per million tokens, input / output):

| Model | Price | Notes |
|---|---|---|
| **`zai/glm-5.3-flash`** (recommended default) | 0.158 / 0.525 | 1M context. minirouter's own docs use it for **both** `web_search` and `news_search`, so it's on a Vercel route and every server tool works. |
| `anthropic/claude-haiku-5.5` | 0.105 / 0.525 | 1M context; strong at tool use. Vercel routing unknown, so news search may not work. |
| `openai/gpt-6-luna` | 0.105 / 0.525 | 1M context; supports structured output. Vercel routing unknown. |
| `google/gemini-2.5-flash-lite`, `openai/gpt-5-nano` | ≤ 0.105 / 0.42 | Cheaper fallbacks; weaker at multi-step tool use. |

**Test results, 2026-11-20** (`scripts/research_smoke_test.py`, $0.12 total including the diagnosis above):

| | GLM-5.3-Flash | Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|
| Forced function call + round trip | ok | ok | ok |
| Planted clash (round during the window) classified as | `hard_round_conflict` ✓ | `rumoured` ✗ | `hard_round_conflict` ✓ |
| Google search mixed with our function | ok (once `max_tool_calls` removed) | not retested | not retested |
| News search | ok, $0.013 | ok, $0.013 | ok, but $0.028 (exceeded `max_uses`) |
| Standalone web fetch | ok (key-level, not per model) | | |

**Decision: `zai/glm-5.3-flash`.** It classified the clash correctly, kept to its search allowance, and is confirmed to support every server tool. GPT-6 Luna is the backup. Haiku's under-classification is safe in itself, but it would weaken the halt, which only acts on hard categories. A side observation from the search probes: the leads Google returned were poor (a local weekend Swiss, an elite event cited from an unofficial calendar). That confirms the design choice that search only *points* the model somewhere, and findings must rest on a fetched page that passes the grounding check.

`scripts/research_smoke_test.py` tests a model on exactly these features (forced function call, tool-result round trip, forced `submit_findings` on a planted clash, Google search mixed with a function, news search, standalone web fetch) and prints the cost of each. Pick the model from its results, not from this table.

**Estimated cost per full run** (not skipped by the delta check): ~4 map loops × ~4 turns × ~15k input tokens ≈ 250k input + 10k output ≈ $0.05 on GLM-5.3-Flash, plus ~8 Google searches ($0.08) and ~15 page reads ($0.015). **About $0.15, and searches cost more than the tokens.** At ~10 full runs per market per week, that's roughly $1.50 per market per week.

### 8.5 Configuration

| Env var | Where | Purpose |
|---|---|---|
| `MINIROUTER_KEY` | `.env.local` locally (gitignored, already loaded by `src/db.py`); tt-worker on Railway | API key |
| `MINIROUTER_BASE_URL` | optional | Defaults to `https://api.minirouter.sh/v1` |
| `RESEARCH_MODEL` | tt-worker | Map-loop model (default `zai/glm-5.3-flash`) |
| `RESEARCH_REDUCE_MODEL` | optional, unset | Model for the single reduce call; defaults to `RESEARCH_MODEL`. **Decided: GLM-5.3-Flash for reduce too**; set this only if reduce quality turns out poor. |

**Key settings in the minirouter dashboard:** use a dedicated key for the researcher and set `dailyLimitNano` / `monthlyLimitNano` spend caps on it as a backstop that holds even if our code's budget has a bug. **Don't set `allowedModels`.** The docs say keys with model restrictions can't use the standalone web-fetch endpoint, which `fetch_page` depends on.

### 8.6 Budgets

Per run: a maximum of 40 function-tool calls across all map loops, 10 searches, a token cap, a dollar cap (default $0.30) from the summed `usage.cost`, and the 15-minute ops timeout. Hitting a cap ends the map stage and goes straight to reduce with what it has, flagged `budget_exhausted` on the proposal. A weekly dollar cap per market in `research_control` pauses scheduling if exceeded.

## 9. Dashboard

The Attendance page becomes three sections (cards stacked on mobile):

**Halt banner** (app-wide, every page): after a researcher halt, "Trading halted by researcher" with player, event and sources, plus **Review**, which opens the urgent item. In `shadow` mode the banner says "Researcher would have halted" instead.

**1. Researcher** (top)
- Halt mode selector (`off` / `shadow` / `live`) next to Pause, with the halt thresholds in a collapsed settings panel.
- Status line: last run (time, result: `proposal` / `no changes` / `skipped (unchanged)` / `failed`), next scheduled run, this week's spend. Buttons: **Run now** (with the one-off context box), **Pause**. Tapping the status opens the run in the job drawer with the live tool-call log.
- **Pending proposal** as one card per item: player, current working value → proposed value, expected fair-value impact in cents on the player's main contract, category chip, confidence, a one-line rationale with expandable sources, and Accept / Edit / Reject. Collapsed groups for `already applied` and `previously rejected`. **Accept all**, **Reject all**, and a **Recompute after applying** checkbox (on by default).

**2. Working adjustments** (the existing form)
- Unchanged, plus a provenance badge on each entry (`manual` / `researcher #id`) showing the note on tap, and the version check on save.

**3. Context**
- Standing notes (add / edit / expire).
- Event catalog: upcoming detected events sorted by overlap with the event window, their leads, and the policy selector per event.

New API routes (under `/api/markets/{slug}`): `GET research` (status, pending proposal, recent runs), `POST research/run`, `PUT research/control`, `POST proposals/{id}/decide`, `GET|POST|PUT|DELETE research/context`, `GET research/events`, `PUT research/events/{id}/policy`.

The Streamlit app gets nothing; it's Titled Tuesday only and being replaced.

## 10. Data model

Created by `research.store.init_schema()` at API and worker start, like the `mm_*` tables, except the `trade_jobs.lane` change (§2.2), which needs the migration.

```sql
research_control (market PK, enabled, paused, params JSON, updated_at, updated_by)
  -- params: intervals, candidate threshold/top-K, budgets, category→adjustment defaults

research_runs (
  id PK, market, tourn_date, job_id → trade_jobs.id,
  status        -- running | proposal | no_changes | skipped_unchanged | failed | budget_exhausted
  input_hash, prompt_version, model, reduce_model,
  dossier JSON, transcript JSON, adhoc_context TEXT,
  tool_calls INT, searches INT, page_reads INT, input_tokens INT, output_tokens INT, cost_usd REAL,
  provider TEXT, balance_usd REAL, collector_errors JSON,
  error TEXT, started_at, ended_at)

research_halts (
  id PK, market, tourn_date, run_id, item_id, player_name, event_id,
  mode            -- shadow | live
  outcome         -- halted | would_halt | declined
  failed_gate TEXT NULL, move_c REAL, exposure_usd REAL, kill_job_id NULL → trade_jobs.id,
  reason TEXT, sources JSON, created_at, resolved_at, resolution   -- accepted | false_alarm
  UNIQUE (market, tourn_date, player_name, event_id, mode) WHERE outcome <> 'declined')

research_events (
  id PK, market NULL (= all), name, normalized_key UNIQUE, source_urls JSON,
  start_date, end_date, rounds JSON, participants JSON,
  policy, policy_value REAL, policy_players JSON, policy_note TEXT,
  first_seen_at, last_seen_at)

research_context (
  id PK, market NULL, text, players JSON, event_id NULL → research_events,
  active_until DATE NULL, created_at, created_by)

adjustment_proposals (
  id PK, market, tourn_date, run_id → research_runs,
  status        -- pending | decided | superseded
  summary TEXT, created_at, decided_at)

adjustment_proposal_items (
  id PK, proposal_id, player_name, username,
  adjustment_type   -- cut | keep | override | nudge | clear
  value REAL, category, confidence REAL, rationale TEXT, sources JSON, event_id,
  fv_impact_c REAL,
  flag              -- NULL | already_applied | previously_rejected
  decision          -- NULL | accepted | edited | rejected
  applied_type, applied_value, decision_reason TEXT, decided_at, decided_by,
  carried_from_item_id NULL)

adjustment_rejections (
  id PK, market, tourn_date, player_name, adjustment_type, value, sources JSON,
  reason TEXT, item_id, created_at)

adjustment_versions (market, tourn_date, version INT, PRIMARY KEY (market, tourn_date))
```

`manual_adjustments` keeps its schema. Applied rows use `created_by='researcher:<proposal_id>'` and `note=<rationale>`.

Module layout:

```
src/research/
    config.py       ResearchParams (pydantic, extra='forbid'), category defaults
    candidates.py   stage 1
    collectors/     chesscom_events.py, lichess_broadcasts.py, chess_results.py, fide.py
    overlap.py      window test, ConflictLead
    agent.py        map/reduce loops, tool implementations, budgets
    halt.py         halt gates (pure) + enqueue_kill call, shadow/live
    llm.py          ChatModel adapter (minirouter.sh), fetch_page via /v1/tools/web-fetch
    policy.py       findings → items (pure)
    proposals.py    supersede/carry-over, diff vs working set, decide/apply
    store.py        tables, init_schema
    prompts/        versioned prompt files
scripts/research_attendance.py   ops-job entry point
scripts/migrate_research.py      lane CHECK migration
scripts/research_smoke_test.py   minirouter model check (exists)
```

## 11. Safety, cost and failure modes

| Risk | Mitigation |
|---|---|
| Prompt injection from fetched pages | The only side effect is the halt. Everything else is a proposal that needs your approval. Strict schema; values bounded in code; the grounding check; the system prompt marks tool output as data. |
| False halt (hallucination, injection, misread time zone) | Six code gates (§7.2), including a grounded hard category and real exposure; shadow mode first; one halt per (player, event); resuming is always manual. Worst case is cancelled quotes until you resume: lost edge, never a position. |
| Halt arrives too late | Gates are checked after each map loop, not at run end; the kill job uses the priority lane; runs every hour in the final 12 hours. Order expiry at the MM cutoff remains the final protection. |
| An event appears on chess.com before anywhere else | You paste chess.com events from the dashboard; the proposal shows how old the last paste is; Lichess usually lists elite events (with round times) before they start; agent searches find chess.com event pages through Google's index. |
| Hallucinated conflict → wrong cut on a favourite | Category → number mapping in code; grounding check downgrades unsupported claims; per-item review with sources one tap away. |
| Researcher misses a conflict | The base model and your manual workflow are unchanged; the researcher only adds. Evaluation (§12) measures misses. |
| Proposal spam every 3 hours | Delta check, carry-over of undecided items, collapsing of `already_applied` and `previously_rejected`. |
| Runaway cost | Per-run and per-week dollar caps, delta skipping, budgets per map loop. |
| Blocking trades | Background lane, subprocess, timeout. |
| Source breaks (layout change, rate limit) | Each collector fails on its own; the run continues with the rest and the proposal lists which sources failed. Three consecutive failures of one collector raise an alert. |
| Stale proposal applied after the data moved on | Version check on decide; proposals are scoped to (market, date) and all pending ones expire at the event start. |
| Router outage | The run fails visibly in the Logs page; scheduling continues; the deterministic leads (stage 2) still show in the event catalog, so you can act on them by hand. |

## 12. Evaluation

The researcher only pays off if its proposals beat no proposals. After each event the weekly scrape knows who actually played, so:

- **Per item:** did a cut or negatively nudged player attend? Did a `confirmed_playing` player attend? Accuracy by category and confidence bucket. This tunes the category defaults in §3, stage 4 with data rather than guesswork.
- **Per player-event:** Brier score of `p_participate` raw vs working-set-before-proposal vs proposal-as-written vs as-applied. That separates the researcher's value from your edits.
- **Misses:** top candidates who didn't play and had no proposal item; inspect the run's leads to see whether collectors or reasoning failed.
- **Halts:** every `halted` and `would_halt` row checked against whether the player played. Precision decides whether `live` mode stays on and where the thresholds sit.
- **Cost per useful item.**

A `research_outcomes` view joins items to standings. A new section in `notebooks/model-performance-audit.ipynb` reads it.

## 13. Phased roadmap

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Spikes** ✓ done 2026-11-20 | Model check: `zai/glm-5.3-flash` chosen (§8.4). chess.com probe: challenged, ruled out (§3, stage 2). Lichess API confirmed as the automated calendar. | Done. |
| **1. Deterministic layer** ✓ built 2026-11-20 | Background lane + migration; candidates; Lichess collector (every tier); clash test; event catalog with per-event policies; the research job without an LLM; Research section on the Attendance page (leads, events, policies, Run now) with a manual "Add an event" form. **Deferred to 1b:** chess-results and FIDE collectors. **Moved to phase 2:** the paste-and-extract box for chess.com (it needs the LLM). | First live run (24 Nov): 40 candidates, 45 events, 9 leads, including Maghsoodloo and Deac in the Spanish team championship, whose Tuesday round overlaps Titled Tuesday, and Erigaisi, Nepomniachtchi and Niemann in the Jerusalem Masters. |
| **2. LLM + proposals** | minirouter adapter, `fetch_page`, map/reduce agent, policy, proposals with per-item decide, merge-apply, version check, Proposals UI. **Run now** only, no schedule. | Two or three events of manual runs where the proposals look sane. |
| **3. Autonomy + halt in shadow** | Scheduler, delta check, standing notes, event policies, rejection memory, budgets, alerts. Halt gates and banner in `shadow` mode. | Runs unattended for two weeks within budget, no noisy re-proposals, and every "would have halted" checked by hand. |
| **4. Live halt + measurement** | Switch halt to `live`; outcome view and audit notebook; tune category defaults and halt thresholds from data. | Measured Brier improvement over raw `p_participate` on researched players; no false live halts. |

Bullet Brawl gets the same machinery with its own window. An arena allows joining late, so a one-hour clash may only reduce games played rather than prevent attendance, and its category defaults should be gentler (`hard_round_conflict` → nudge, not cut). That's why the policy table is per market.

## 14. Open questions

Resolved since v1: minirouter is OpenAI-compatible with function calling and per-request cost (§8); search comes from its server tools, so no separate search key; the model is `zai/glm-5.3-flash` (§8.4); chess.com/events can't be read automatically, so it's pasted in and Lichess is the automated calendar (§3, stage 2); high-impact findings trigger a gated market halt (§7).

**Decided 2026-11-20:**

1. **Lichess:** collect every tier; a top player can enter a minor open (§3, stage 2).
2. **Placement:** sections on the Attendance page.
3. **Candidates:** raw-model top-8 fair value above 5% (§3, stage 1).
4. **Halt:** confidence ≥ 0.8 and exposure ≥ $50 as proposed. The move threshold *is* the market maker's `buffer_c`, whose default rose from 1¢ to 2¢ (done in `src/mm/config.py`), so scouts still quote on contracts above about 6¢ fair. A small buffer means more halts when news breaks; that trade-off is intended (cautious).
5. **Reduce model:** GLM-5.3-Flash throughout; upgrade only if results disappoint.

No open questions remain before phase 1.
