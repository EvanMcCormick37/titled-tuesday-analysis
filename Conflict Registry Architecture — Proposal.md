Architecture proposal · Kalshi automated trading

# Conflict registry: one source of truth for attendance conflicts across markets

22 November 2026 · Draft v2 for review

**Changes since v1:** the conflict effect is a **learned log-odds adjustment** from a walk-forward model of conflict features, not a hand-set table of `q` values (§7). First evidence from history is in §7.1. Collection runs only up to **each market's next event**, not 14 days ahead, but stores every tournament it finds in full (§5). Every unfinished tournament in the registry is **re-checked on each run** for new players and schedule changes (new §5.4). Performance effects stay out of scope, and global jobs use `market = '_global'`. All five v1 open questions are resolved (§14).

**Contents**

1. [Summary](#1-summary)
2. [What exists today, and what's missing](#2-what-exists-today-and-whats-missing)
3. [Core idea: three layers](#3-core-idea-three-layers)
4. [Data model](#4-data-model)
5. [The lookahead job](#5-the-lookahead-job)
6. [The clash test, generalised](#6-the-clash-test-generalised)
7. [The conflict model: conflicts → log-odds adjustments](#7-the-conflict-model-conflicts--log-odds-adjustments)
8. [How compiled adjustments reach the model](#8-how-compiled-adjustments-reach-the-model)
9. [Dashboard](#9-dashboard)
10. [Feedback loop: registry → history](#10-feedback-loop-registry--history)
11. [Relationship to the LLM researcher proposal](#11-relationship-to-the-llm-researcher-proposal)
12. [Failure modes and safeguards](#12-failure-modes-and-safeguards)
13. [Phased roadmap](#13-phased-roadmap)
14. [Open questions](#14-open-questions)

## 1. Summary

Today attendance conflicts are entered **per market, per event, per player**, by typing cuts and nudges into each market's Attendance page. One tournament, such as a team championship with a Tuesday and a Friday round, becomes six or more separate manual edits that can disagree with each other. This proposal moves the unit of work from *the adjustment* to *the tournament*:

- **One cross-market registry of tournaments.** Every tournament found by the Lichess collector or added by hand appears once, with its round schedule, its entry list and a review status. It is shared by all markets.
- **Clashes are computed, not typed.** For every tournament round and every upcoming chess.com occurrence (TT, BB, FF), code records the overlap in minutes, or the gap.
- **You review tournaments and entries, not adjustments.** You approve a tournament once, add the players you know are in it, mark it a team event if it is one, and that decision applies to every market it touches.
- **A learned conflict model** turns approved conflicts into per-market, per-player log-odds adjustments. It is trained on years of historical conflicts (Lichess PGN dumps against our standings). The Attendance page shows them at the top as a read-only layer, and your manual adjustments stay below as the final per-event filter, overriding anything compiled.

Main design decisions:

| Decision | Why |
|---|---|
| **Extend `research_events`; don't build a second catalog.** It is already cross-market (no `market` column), keyed by (source, source_id), and preserves operator decisions across re-collection. | Avoids two tables that drift apart. The researcher shipped on 20 Nov, so the migration is small. |
| **Three entities: tournaments, entries, clashes.** Player conflicts are a join of the three and are never stored by hand. | Un-approving a tournament, removing a player or a schedule change updates every market automatically, with nothing left stale in `manual_adjustments`. |
| **Approval works at two levels.** The tournament is approved ("this schedule is real and relevant") and each entry has its own status ("this player is in it"). | Lichess often publishes the field only at round 1. You can approve a tournament on Monday and add players on Wednesday without approving it twice. |
| **The effect is a learned log-odds shift Δ(features)**: `logit p_new = logit p_raw + Δ`. Δ comes from a logistic model fitted with the player's walk-forward `p_participate` as an offset, using features such as market, round timing relative to our window, round position in the tournament, team format, field strength and the player's rating edge over the field. | How much a clash matters depends on the market, the tournament, the round and the player. History has tens of thousands of labelled (player, conflict week) pairs, and §7.1 shows the effect is large and varies with timing. Log-odds is the natural scale: Δ is an additive term the model estimates directly, and it plugs into the existing nudge mechanism. |
| **Compiled adjustments are a separate layer, computed at `run_adjusted` time**, never written into `manual_adjustments`. | Nothing has to be kept in sync: what you see is what the next recompute applies. Your manual rows keep their meaning. |
| **One lookahead job for all markets**, run after every weekly pipeline and once a day, collecting up to each market's next event and re-checking every unfinished tournament already registered. | It replaces three per-market research runs that each looked at ±1 day around one event. One collection serves all three markets, and entry lists fill in as Lichess publishes them. |

## 2. What exists today, and what's missing

You asked whether a conflicting-tournaments table already exists. Partly: there are three related stores, built for different jobs.

| Store | What it holds | Limits for this workflow |
|---|---|---|
| `research_events` (+ `src/research/`) | Cross-market catalog of tournaments from the Lichess broadcast API and the "Add an event" form: rounds, players, and a per-event `policy` (`auto / ignore / nudge_only / cut / override`) | Collected only within `calendar_days` (1) of **one** market's next event. Clashes and leads are computed per run and saved as JSON on `research_runs`, so they can't be queried or reviewed per row. Entry lists from Lichess are overwritten on every refresh, so you can't add players to a Lichess event. No review status, no team flag, and nothing flows into the model: leads are advisory only. |
| `other_events` / `other_event_rounds` / `other_event_participants` | **Historical** OTB tournaments parsed from Lichess monthly PGN dumps | Backward-looking (games already played). Feeds `attendance_conflicts`. Not useful for upcoming events. |
| `attendance_conflicts` | Past (market, date, player) conflict weeks | Used by `load_and_prepare()` to take conflict weeks out of the participation denominator, so `p_participate` is a conflict-free attendance rate. This is why a forward conflict adjustment doesn't double-count (§7). |

So the missing pieces are: a **multi-market horizon**, **re-checks of known tournaments**, **materialised clash rows**, **operator-maintained entry lists**, **review status**, a **team format**, and a **conflict model** whose output actually feeds predictions.

## 3. Core idea: three layers

```
   COLLECT (code)                 REVIEW (you)                  APPLY (code)
 ┌─────────────────────┐     ┌──────────────────────────┐    ┌───────────────────────────┐
 │ Lichess broadcasts  │     │ Conflicts page (global)  │    │ compiler (pure function)  │
 │ + manual tournaments│────▶│  approve / dismiss       │───▶│  per market × occurrence  │
 │                     │     │  format: individual/team │    │  × player                 │
 │ lookahead job       │     │  entries: add / confirm /│    │  → Δ (log-odds), reasons  │
 │  → research_events  │     │   reject false matches   │    │  (conflict model, §7)     │
 │                     │     │                          │    └─────────────┬─────────────┘
 │                     │     │                          │                  │
 │  → event_clashes    │     └──────────────────────────┘                  ▼
 │  → tournament_      │                                     Attendance page, per market
 │    entries (auto)   │                                      ┌ From conflicts (read-only,
 └─────────────────────┘                                      │   suppress per player)
                                                              └ Manual adjustments (final
                                                                  filter, always wins)
                                                                         │
                                                                         ▼
                                                                    run_adjusted
```

Example: you approve the Spanish Team Championship, mark it `team`, and confirm Maghsoodloo and Deac. The compiler emits, with no further typing (illustrative numbers):

| Market | Date | Player | Clash | Δ (log-odds) | p: raw → adjusted |
|---|---|---|---|---|---|
| TT | 24 Nov | Parham Maghsoodloo | R3 overlaps 105 min (team, round 3 of 9) | −1.9 | 0.82 → 0.41 |
| TT | 24 Nov | Bogdan-Daniel Deac | R3 overlaps 105 min (team, round 3 of 9) | −1.9 | 0.55 → 0.15 |
| FF | 27 Nov | Parham Maghsoodloo | R6 overlaps 40 min (team, round 6 of 9) | −1.2 | 0.70 → 0.41 |

## 4. Data model

All new tables and columns go in `src/research/store.py`'s DDL. New tables are created by `init_schema()` as today. The new columns on `research_events` need a one-off `scripts/migrate_conflicts.py` (idempotent, same pattern as `migrate_research.py`), run against Railway before deploying.

### 4.1 `research_events`: the tournament (extended)

New columns:

| Column | Type | Meaning |
|---|---|---|
| `review_status` | TEXT, default `new` | `new` → `approved` / `dismissed`. Only `approved` tournaments compile into adjustments. |
| `reviewed_at`, `reviewed_by` | ts, TEXT | Audit |
| `schedule_hash` | TEXT | Hash of `rounds` at approval. A later collection that changes the schedule sets `schedule_changed = 1`. |
| `schedule_changed` | INTEGER | Shows the tournament in "Needs review" again without un-approving it. The compiler keeps using the new schedule; the flag only asks you to check it. |
| `format` | TEXT, default `individual` | `individual` or `team`. A **model feature**, not a rule. Pre-filled from source data where possible (§7.3), and you correct it. |
| `n_rounds` | INTEGER | Total rounds, for the round-position feature. Taken from the Lichess round list; editable on hand-added tournaments. |
| `delta_override` | REAL, nullable | Escape hatch: a fixed log-odds shift for every entrant, replacing the model for this tournament |
| `note` | TEXT | Free text: why it was approved, sources |

The existing `policy` / `policy_value` columns are absorbed: `ignore` becomes `review_status = dismissed`, and `nudge_only` becomes `delta_override`. `cut` and `override` have no direct equivalent and are converted to a large negative `delta_override` (−6, which is a cut in practice). The migration converts any existing rows.

`OperatorEvent` (the "Add a tournament" form) also gets an **explicit round list** (date + time per round, with rest days and double rounds) as an alternative to today's "same time every day" rule, which doesn't fit most real schedules.

### 4.2 `tournament_entries`: who is playing (new)

One row per (tournament, player we care about).

| Column | Meaning |
|---|---|
| `id`, `event_id` (FK `research_events`) | |
| `player_name` | Our display name from `player_information` (what Kalshi and `manual_adjustments` use). NULL if the entrant couldn't be matched. |
| `entrant_name` | The name as the source lists it ("Maghsoodloo, Parham") |
| `source` | `lichess`, `operator`, later `llm` |
| `match` | `exact / strong / partial / hint / operator` (from `names.py`) |
| `status` | `confirmed`, `probable`, `rejected` (false match), `withdrawn` |
| `rating` | The entrant's rating as listed (Lichess players endpoint, or typed in). Feeds the rating-edge feature. |
| `delta_override` | Optional per-player log-odds override, e.g. a reserve in a team event who rarely plays |
| `in_source` | 1 if the latest collection still lists this entrant. A Lichess entry that disappears is flagged for review, not deleted. |
| `note`, `created_at/by`, `updated_at/by` | |

Unique on `(event_id, player_name)` when `player_name` is set, and on `(event_id, entrant_name, source)` otherwise.

**Status defaults:** a Lichess entrant matched `exact` or `strong` starts `confirmed`; `partial` and `hint` start `probable`; operator-added entries start `confirmed`. Re-collection inserts new entrants and refreshes `in_source`, but **never changes a status you set**. That rule is what makes manual players safe on a Lichess tournament.

Matching runs against **tracked players**: the union of every registered market's raw candidates (§5.3), plus anyone with an existing entry. The full roster stays in `research_events.players`, so when the candidate set changes, entries can be re-derived without refetching.

### 4.3 `event_clashes`: tournament round × our occurrence (new, materialised)

Player-independent, recomputed in full for a tournament whenever its schedule or our schedule changes.

| Column | Meaning |
|---|---|
| `event_id`, `round_name`, `round_index`, `round_start`, `round_end_assumed` | The round (`round_index` is 1-based; with `n_rounds` it gives round position). The end time is assumed from the time control (`round_minutes`) and stored so the UI can say "assuming a 5 h round". |
| `market`, `occurrence_date`, `occurrence_start`, `occurrence_end` | The chess.com event, from `MarketSpec.start_ts` + `event_duration_minutes` |
| `kind` | `overlap`, `adjacent`, `mid_event` (today's `hard / adjacent / multi_day`, renamed so the minutes carry the severity) |
| `overlap_minutes` | For `overlap` |
| `gap_minutes` | For `adjacent` / `mid_event`: signed, negative means the round ends before our start |
| `computed_at` | |

Unique on `(event_id, round_name, market, occurrence_date)`. Untimed tournaments (dates but no round times) get a single `mid_event` row per occurrence they span, flagged `untimed`, as `classify()` does today.

### 4.4 Player conflicts: a view, not a table

```sql
-- conceptually
SELECT c.market, c.occurrence_date, en.player_name, ev.id, ev.format, c.kind,
       c.round_index, ev.n_rounds, c.overlap_minutes, c.gap_minutes, en.status, en.rating,
       COALESCE(en.delta_override, ev.delta_override)
  FROM event_clashes c
  JOIN research_events ev     ON ev.id = c.event_id AND ev.review_status = 'approved'
  JOIN tournament_entries en  ON en.event_id = ev.id AND en.status IN ('confirmed','probable')
 WHERE c.occurrence_date >= current_date
```

This is the conflict table you sketched in point 2 (round time, our event and its date and time, degree of conflict). Because it's a view, it can never disagree with the tournaments and entries underneath it.

### 4.5 `conflict_control` and `conflict_applied` (new, small)

- `conflict_control`: one row per market (plus a `_global` row for horizon and collector settings). Params JSON validated by a pydantic `ConflictParams` (`extra='forbid'`), the same pattern as `research_control` and `mm_control`. Holds the active conflict-model version, `probable_weight`, the clash-window settings and the Lichess collector settings.
- `conflict_models`: one row per fitted model: version, fitted_at, git commit, training window, coefficients JSON (or a pickled artifact path), and walk-forward scores. Production reads the version named in `conflict_control`, so a refit never changes live prices until you promote it.
- `conflict_applied`: (market, tourn_date) → `compile_hash`, `items` JSON, `applied_at`. Written by `run_adjusted`, so the dashboard can tell whether what's on screen is what the model last used (§8.3), and so every official prediction records which conflicts produced it.

## 5. The lookahead job

### 5.1 What it does

```
 horizon      = [now, latest of each registered market's NEXT event end]
 discover     = Lichess listings (top + official), all tiers, tournaments with any day in the horizon
 recheck      = every registry tournament not yet finished (§5.4), listed or not
 upsert       → research_events            (facts only, ALL rounds even beyond the horizon;
                                              review fields untouched)
 match        → tournament_entries         (tracked players; never overwrites status)
 clash        → event_clashes              (per tournament, wipe and replace)
 flag         → schedule_changed, new tournaments with any clash, new / dropped entrants
 summary      → lookahead_runs row          (stats, collector errors, what's new)
```

**Horizon = through each market's next event.** On Sunday that is TT Tuesday, FF Friday and BB Saturday, i.e. the current trading week. Next week's markets don't open until this week's close, so looking further ahead adds requests without adding decisions. Discovery stops at the horizon, but **a tournament that's found is stored in full**: a nine-round event starting Thursday keeps its rounds for the following Tuesday. Its clashes with that Tuesday are computed when the next run's horizon reaches it. Tournaments you add by hand are never limited by the horizon (e.g. an Olympiad announced months ahead).

Player lists and round details are fetched for every tournament with at least one clash row. Tournaments with no clash are stored but not detailed, so the request count scales with relevant events rather than with everything on Lichess.

### 5.2 When it runs

| Trigger | Why |
|---|---|
| End of every weekly pipeline (replaces the per-market `research` step; still advisory, never fails the pipeline) | Fresh candidates exist. The step can be called once for each of the three pipelines; it simply refreshes. |
| Daily at 06:00 ET (tt-worker periodic duty, DB-timestamped like the snapshots) | Lichess publishes entry lists around round 1, often midweek. |
| T − 4 h before each occurrence | Last chance to catch a newly published field before the MM cutoff. |
| **Run now** on the Conflicts page | |

It runs in the trade queue's `background` lane (the job drawer, logs and cancel work as they do today, and it never blocks a trade lane) under the reserved **`market = '_global'`** (decided). The ops registry learns that key; the Logs page shows it as "All markets". `trade_jobs`' partial unique index only covers the normal lane, so `_global` should not collide, but that needs a test.

### 5.3 Candidates across markets

Tracked players = the union over markets of `load_candidates(spec, params)` (raw top-8 fair value > 5% by default), plus every `player_name` already in `tournament_entries`. A player who matters only in Bullet Brawl is still tracked, and a conflict found for them applies everywhere they're priced.

### 5.4 Re-checking tournaments already in the registry

Yes: every run re-checks what's already registered, which is how entry lists fill in after round 1. Today's collector only refreshes tournaments that still appear in Lichess's listings near one date, and `/api/broadcast/top` drops events as they age, so a known tournament can silently stop updating. The lookahead fixes that with an explicit re-check set:

| Re-checked every run | How |
|---|---|
| Every `lichess` tournament in the registry that is `new` or `approved` and not finished (`end_date ≥ today`, or any round unfinished), **whether or not it's still listed** | Fetched directly by id: `GET /api/broadcast/{id}` (rounds) and `GET /broadcast/{id}/players` (field) |
| Dismissed tournaments | Not re-checked (no requests spent), unless they reach the horizon again with a schedule change, which shows up as a listing change |
| Hand-added tournaments | No source to re-check; you edit them. If one later appears on Lichess (§5.5), it can be linked so it gets re-checked from then on. |

What a re-check may change, and what it never touches:

| Updated from the source | Never changed by a re-check |
|---|---|
| Round list and start times. A change to an **approved** tournament sets `schedule_changed`, which puts it back in *Needs review*; the new times are used immediately and the compile hash changes (§8.3). | `review_status`, `format`, `delta_override`, `note` |
| The full roster in `research_events.players`. New entrants matching a tracked player become new `tournament_entries` rows (confirmed or probable by match grade) and appear in *Needs review* as "2 new entrants". | Status of any existing entry: your confirmations, rejections ("not this player") and withdrawals stand |
| `in_source` on existing Lichess entries. An entrant who disappears is flagged "dropped from Lichess list", not deleted or auto-withdrawn. | Players you added by hand (`source = operator`), even if Lichess never lists them |
| Ratings, titles, `n_rounds`, name, dates | |

Two refinements:

- **Re-matching without refetching.** When the tracked-player set changes (a new pipeline run raises someone's fair value above the threshold), entries are re-derived from the stored roster, so a player who just became relevant is matched against fields collected days ago.
- **Your manual player plus a later Lichess listing** become one entry: the Lichess entrant matches the existing `(event_id, player_name)` row, which keeps your status and gains `in_source = 1` and the listed name and rating.

### 5.5 Linking a hand-added tournament to Lichess

A special event you added by hand sometimes appears on Lichess later. The re-check flags a likely duplicate (same dates, name similarity, shared players) and offers **Link**. Linking attaches the Lichess id to your row: your review fields, entries and notes stay, and from then on the round list and roster refresh from Lichess. Rounds you typed are replaced by Lichess's, and the change is flagged as a schedule change.

## 6. The clash test, generalised

`overlap.classify()` already computes overlap minutes against one window. The change is to loop over **all occurrences in the horizon** and emit one row per (round, occurrence) instead of one verdict per tournament. The thresholds stay in params (`adjacent_hours` 4, `mid_event_hours` 36, round lengths by time control).

Two additions:

1. **Per-occurrence schedule overrides.** Freestyle Friday has started at 12:00 ET instead of 11:00 on five US/EU daylight-saving mismatch weeks, and an hour shift changes overlap minutes. Add a small `event_schedule_overrides (market, date, start_local, cancelled)` table, editable on the Conflicts page, read by the occurrence generator. Cancelled occurrences get no clashes.
2. **Assumed round end shown, not hidden.** Classical rounds run long. The UI shows "R5 15:00–20:00 CET (assumed 5 h)", so you can judge a 20-minute overlap yourself.

## 7. The conflict model: conflicts → log-odds adjustments

How much a clash costs depends on the market (Bullet Brawl draws less than Titled Tuesday), the tournament (a club open versus a super-tournament), the round (a super-GM can finish round 2 of an open quickly against a weak opponent and still make the 11:00 start, but not the final round), and the player. A table of hand-set `q` values can't capture that. History can: years of Lichess PGN dumps (`other_event_*`) and our standings give tens of thousands of labelled (player, event week, conflict) pairs. So the effect is **learned**.

### 7.1 Evidence that the signal is there

A quick probe on the local snapshot (Titled Tuesday, 2023 onwards). Pairs come from `attendance_conflicts` (a round of a tournament the player played in, on the same UTC date as TT), with the round time taken from the nearest round's earliest game. The baseline is the same players' overall attendance rate since 2023, so it includes conflict weeks and understates the conflict-free rate:

| Nearest round start, relative to TT start | All players: n · played · baseline | Regulars (baseline ≥ 30%): n · played · baseline |
|---|---|---|
| > 5 h before | 7,291 · 3.3% · 10.5% | 673 · **15.0%** · 45.2% |
| 2–5 h before (classical round likely still running) | 10,305 · 2.2% · 11.4% | 1,190 · **8.8%** · 43.9% |
| < 2 h before | 4,311 · 1.6% · 10.0% | 374 · **8.3%** · 44.2% |
| 0–2.5 h after (inside the TT window) | 2,365 · 3.2% · 10.0% | 220 · **17.3%** · 44.3% |
| > 2.5 h after | 2,873 · 4.8% · 9.9% | 260 · **16.5%** · 42.8% |

For regulars, a round 2–5 h before TT takes attendance from about 44% to about 9%, a log-odds shift of roughly −2.1. A round more than 5 h before, or one starting during TT, costs about −1.5. So **the effect is large, it varies by timing, and none of these cases is a certain cut.** Caveats: the probe uses 16:00 UTC as TT's start for every week (an hour off in summer), takes no account of round number, format or rating, and matches names exactly after normalisation. It is a feasibility check, not the model.

### 7.2 Target and formulation

The unit is **(player, market occurrence)**: one row per player per chess.com event where the player has at least one round within the `mid_event` window. Label: did the player play (`played_filter` for that market).

```
logit P(play) = logit(p_base)  +  Δ(x)
                └─ offset ─┘       └─ learned: the conflict effect ─┘
```

- **`p_base`** is the player's walk-forward `p_participate` for that week, the same conflict-free rate production computes (`load_and_prepare(as_of=)`). It enters as an **offset**, so the model learns only the conflict effect, and Δ is directly the log-odds shift production applies. That answers your log-adjustment question: yes, log-odds is the right scale. It matches the existing nudge mechanism, and an additive Δ on log-odds behaves sensibly for both a 90% regular (big absolute drop) and a 10% occasional player (small absolute drop).
- **Several clashes for one player and occurrence** (two rounds, or two tournaments) are summarised into one row's features (most constraining round, plus counts), not combined afterwards. The training data contains such cases, so the model learns how they combine.
- **`probable` entries** (uncertain name matches) are a probability mixture: `p = w · p_conflict + (1 − w) · p_raw`, with `w = probable_weight` (default 0.5).
- **Overrides:** a per-entry or per-tournament `delta_override` replaces the model's Δ.

### 7.3 Features

Every feature must be available both historically (from the PGN dumps) and at prediction time (from Lichess or your input). Otherwise the model learns from something it can't see live.

| Group | Feature | Historical source | Live source |
|---|---|---|---|
| **Market** | market (one-hot), interacted with the timing features | spec | spec |
| **Timing** | signed minutes from the nearest round's start to our start (spline: before / during / after behave differently) | `other_event_rounds.earliest_start_utc` | `event_clashes` |
| | assumed overlap minutes, gap minutes | rounds + time control | `event_clashes` |
| | rounds that day (double rounds) | rounds | rounds |
| **Round** | round position `round_index / n_rounds`, and last-two-rounds flag | round number from the broadcast name, or the PGN `Round` tag | Lichess round list |
| **Tournament** | team format | PGN `WhiteTeam` / `BlackTeam` tags | Lichess tour team flag (to verify), or your toggle |
| | time control class, online/OTB | `modal_time_control`, `is_online` | Lichess `fideTC` |
| | field strength: share titled, field size, median rating | `other_events.pct_titled`, `n_players`; Elo tags | Lichess players endpoint |
| | Lichess tier | **not in PGN dumps** | Lichess tour |
| **Player** | rating edge over the field median (the "mops up early rounds" effect), interacted with round position | PGN Elo tags | entry `rating` |
| | `logit(p_base)` as a feature as well as the offset (regulars may work harder to fit the event in) | walk-forward | production |
| | player's own past conflict behaviour, as a per-player intercept shrunk with a field prior (§7.4) | training history | training history |

**Lichess tier** has no history in the PGN dumps. Field strength (share titled, median rating) is a good proxy available on both sides, so v0 uses that. Tier becomes a candidate feature once the registry has a season of live data, or sooner if broadcast names can be mapped to Lichess tours for a backfill.

**Team format** is detected by tag, then keyword, then your toggle (decided):

1. PGN `WhiteTeam` / `BlackTeam` tags (historical) or a Lichess team flag (live), when present.
2. Otherwise, a keyword match on the tournament name, case- and accent-insensitive: `team`, `teams`, `league`, `liga`, `bundesliga`, `olympiad`, `club cup`, `club championship`, `4NCL`, `TCh`, `ETCC`, `Mitropa`, `interclubs`, `CEC`, `superliga`, `extraliga`, `top 12`, `Schachbundesliga`, `Ekstraliga`. The list lives in `src/conflicts/features.py` and is shared by training and live use, so both label the same way.
3. Your toggle on the tournament wins live. Training uses (1) and (2) only, so it matches what the model will see on a tournament you haven't touched.

The keyword list gets one sanity pass in the phase 0 notebook: the 50 most common matched and unmatched names among conflict rows, to catch false positives ("Team" in a sponsor name) and misses.

**Data gaps to fill first:** `other_event_rounds.round` is empty in the current parse, and the parser doesn't keep Elo or team tags. `update_broadcasts.py` needs to keep the PGN `Round`, `WhiteElo` / `BlackElo` and `WhiteTeam` / `BlackTeam` tags, plus a one-off backfill of the monthly dumps **from January 2023** (decided: the tournament scene has changed too much since then for older data to help). `attendance_conflicts` also has **no Bullet Brawl rows** in the local snapshot, so BB's training rows have to be built from the rounds tables directly (which the dataset builder does anyway, §7.5).

### 7.4 Model and evaluation

- **Training window and recency weighting (decided).** Rows from 2023 onwards only, each weighted by an exponential decay on its age at fit time, the same idea as `SKILL_DECAY` and `PARTICIPATION_DECAY`: `w = conflict_decay ^ weeks_before_fit_date`. In walk-forward evaluation the weights are recomputed at every fit date, so no week sees weights from its own future. The decay has to be **mild**: `SKILL_DECAY` (0.96 per week, about a 17-week half-life) would leave too few effective conflict rows for a model with a dozen features. The starting value is a **one-year half-life** (`conflict_decay ≈ 0.9868` per week). The walk-forward comparison chooses among half-lives of 26, 52 and 104 weeks and no decay, and also reports the **Kish effective sample size** for each, so a decay that starves the model is visible.
- **Per-player effect: Freestyle Friday's field-prior shrinkage (decided).** FF's skill model (`skill_prior_events = 3`, `build_player_pool_score`) mixes the field into a player's history with weight `k / (k + n_eff)`. Here the same form applies to a player's conflict intercept:

  ```
  n_eff_i   = (Σ w)² / Σ w²       over player i's past conflict rows, using the recency weights above
  resid_i   = weighted mean of (played − p̂) over those rows, in log-odds   (p̂ = prediction without the player term)
  player_i  = n_eff_i / (n_eff_i + k) × resid_i                            (k = 3 to start)
  ```

  A player with no conflict history gets the population effect only. A player with a long record of fitting TT around classical rounds gets most of their own residual. Like the recency weights, the term is computed only from rows before each fit date. `k` is chosen by walk-forward from {1, 3, 8}, mirroring the `narrow_prior1/3/8` backtest variants.
- **v0: penalised logistic regression with an offset.** Splines on the timing feature, a few deliberate interactions (market × timing, rating edge × round position), recency-weighted rows, and the shrunk per-player term above. It is interpretable: the dashboard can show Δ broken down by feature ("−1.4 timing, +0.5 early round vs weak field, −0.3 team, +0.4 this player"), which matters when you're deciding whether to trust it.
- **v1: gradient-boosted trees** (LightGBM with `init_score = logit(p_base)`, monotone in overlap minutes). Used only if it beats v0 out of sample.
- **Walk-forward by event date:** fit on events before week *t*, predict week *t*, as `run_backtest.py` does. Record each run with its settings and git commit, as `data/backtests.db` does.
- **Scoring:** log loss and Brier on conflict pairs, reported for everyone and for the **VIP cohort** (`src/vip_players.py`, the players actually traded), with bootstrap ranges over events. Baselines: (a) Δ = 0, (b) "cut every same-day conflict" (roughly the manual practice), (c) one constant Δ per timing bucket (§7.1). The model has to beat (c) on the VIP cohort to justify its features.
- **Calibration:** realised versus predicted attendance by Δ decile and by market.

### 7.5 Where it lives

| Piece | Location |
|---|---|
| Training set builder: (player, occurrence) rows from the rounds/participants tables, the walk-forward `p_base` and the labels | `scripts/build_conflict_dataset.py` → `data/conflict_dataset.parquet` (local only) |
| Fit + walk-forward evaluation, writes a `conflict_models` row | `scripts/fit_conflict_model.py --notes "..."` |
| Analysis | `notebooks/conflict-model.ipynb` |
| Feature construction shared by training and production (one code path, so live features can't drift from training features) | `src/conflicts/features.py` |
| The compiler: approved conflicts → features → Δ per (market, date, player), with reasons and feature contributions | `src/conflicts/compile.py` (pure, unit-tested) |

Walk-forward `p_base` needs care: the `backtest` table only has Titled Tuesday from 2025-11, so the builder recomputes the EWMA participation rate as of each week directly. That's cheap (no Monte Carlo), but it must use the same code as production: `load_and_prepare(as_of=)`, or a factored-out piece of it.

### 7.6 Why this doesn't double-count, and one consistency fix

`load_and_prepare()` removes past conflict weeks from the participation denominator, so `p_participate` means "attendance rate on a conflict-free week". Adding Δ for a known conflict week is the correct correction.

One mismatch to fix: today `attendance_conflicts` defines a conflict as "a round on the same UTC date", while the model uses timing windows. A round 30 h away isn't removed from the denominator but is a model row. That's harmless as long as training and production use the same definitions, which they do if `p_base` is computed the production way. Later, `attendance_conflicts` should be rebuilt from the same clash definitions (`overlap` + `adjacent`), so "conflict week" means one thing everywhere.

## 8. How compiled adjustments reach the model

### 8.1 A new adjustment kind: `conflict` (log-odds)

`run_adjusted` gains a `p_conflict: dict[str, float]` argument (player → Δ, already mixed for `probable` entries), applied **with** the existing nudges and **before** overrides, cuts and keeps:

```
p ← sigmoid(logit(p) + global_nudge + nudge + Δ_conflict)   # Δ is new; same mechanism as nudges
override / cut / keep                                        # existing, last word
```

`trigger_adjusted.py` and the recompute ops job call `compile()` for (market, tourn_date) with the promoted model version and pass the result. It is not stored in `manual_adjustments`.

### 8.2 Manual adjustments stay the final filter

Precedence, per player, for one market and event:

| Manual row for this player | Effect on the compiled Δ |
|---|---|
| none | applied |
| `nudge` | both apply (they add in log-odds) |
| `override`, `cut`, `keep` | compiled Δ ignored; shown as *superseded* |
| `ignore_conflict` (new type, no value) | compiled Δ dropped for this player at this event only |

`ignore_conflict` is the one-tap "not for this market" exception: for example, a player you know will play TT between team rounds, while their FF conflict still stands.

### 8.3 Staleness and cross-market recompute

`compile_hash` = hash of the compiled items (and model version) for (market, tourn_date). `run_adjusted` writes the hash it used to `conflict_applied`. The Attendance page and the market's status endpoint compare it with a fresh compile:

- **Banner on Attendance and Pipeline:** "Conflicts changed since the last recompute (Nakamura: −2.1 added)", with a **Recompute** button.
- **Approving a tournament on the Conflicts page** shows what it affects ("TT 24 Nov: 2 players · FF 27 Nov: 1 player") and offers **Recompute affected markets**, which queues the existing `recompute` ops job for each affected market whose upcoming event is in that list and whose pipeline is `adjustments_pending` or `completed`. Each recompute uses its own market's normal lane, as now.
- **Promoting a new model version** changes the hash for every market with conflicts, so it shows the same banner. A refit never moves live prices silently.

### 8.4 Pipeline state

Unchanged in phases 1–2: `run_raw` → `adjustments_pending` → you press Save & recompute. Phase 3 adds an opt-in per market: **auto-complete** when the lookahead found nothing new or changed for that occurrence since your last review. The pipeline then runs `run_adjusted` with the compiled layer itself, which removes the weekly manual step on quiet weeks. It stays off until the compiled layer has a track record.

## 9. Dashboard

### 9.1 New top-level page: **Conflicts** (global)

This reverses the researcher proposal's "sections on the Attendance page" decision, for the reason you gave: the registry is cross-market, so it shouldn't live under one market's picker. The market picker is hidden on this page. Like the rest of the React app, it is designed phone-first: cards, not wide tables.

- **This week strip:** each market's next event as a column (TT Tue · FF Fri · BB Sat). Each tournament card shows chips per event it hits, e.g. `TT 24 Nov · overlap 105 min` (red), `FF 27 Nov · adjacent 2 h` (amber).
- **Tabs:** *Needs review* (new tournaments with a clash, schedule changes, new or dropped entrants, likely duplicates to link), *Approved*, *Dismissed*, *All*. Tournaments without any clash are hidden by default.
- **Tournament detail** (drawer):
  - Schedule in ET, with our windows overlaid, plus the assumed round end
  - Clash rows for every market
  - **Format:** individual / team (a model input), and an optional Δ override
  - **Entries:** auto-matched entrants with match grade and rating; **Add player** (autocomplete from `player_information`, so the match is exact); per-entry status (confirmed / probable / not this player / withdrawn) and optional Δ override
  - **Effect preview:** Δ and p raw → adjusted per market, with the per-feature breakdown ("−1.4 timing · +0.5 early round vs weak field · −0.3 team"), before you approve
  - **Approve** / **Dismiss** / note / source link / **Link to Lichess** (§5.5)
- **Add tournament:** the existing form, extended with an explicit round list and round count (§4.1).
- **Schedule overrides:** shifted or cancelled occurrences (§6).
- **Run lookahead now**, with the last run's summary (new tournaments, re-checked, new entrants).

### 9.2 Attendance page (per market): a new top section

**From conflicts:** one row per player: tournament, clash, Δ, p raw → adjusted, status (*applied / superseded by manual / ignored*). Each row has **Ignore here** (writes `ignore_conflict`) and **Open tournament** (deep link to the Conflicts drawer). The staleness banner (§8.3) sits above it. The manual adjustment editor below is unchanged.

The researcher's current **Research** section (leads list) is replaced by a link: "3 tournaments need review on the Conflicts page".

### 9.3 API

```
GET    /api/conflicts?status=new                    tournaments + clashes + entries + preview
POST   /api/conflicts/lookahead                     queue the lookahead job (market '_global')
PUT    /api/conflicts/events/{id}                   review_status, format, n_rounds, delta_override, note
POST   /api/conflicts/events                        add tournament (OperatorEvent, extended)
POST   /api/conflicts/events/{id}/link              attach a Lichess broadcast id (§5.5)
DELETE /api/conflicts/events/{id}                   operator tournaments only
POST   /api/conflicts/events/{id}/entries           add player
PUT    /api/conflicts/entries/{id}                  status, rating, delta_override, note
GET    /api/conflicts/impact?event_id=              affected (market, date, player, Δ, breakdown)
POST   /api/conflicts/recompute                     {markets: [...]} → recompute jobs
GET    /api/markets/{slug}/conflicts                the compiled layer + staleness for one market
PUT    /api/conflicts/schedule-overrides            shifted / cancelled occurrences
GET    /api/conflicts/models · PUT …/models/active  model versions and their scores; promote one
```

The existing `/research/events/*` routes become aliases during the transition, then are removed.

## 10. Feedback loop: registry → history

The registry produces labelled data the PGN dumps can't: conflicts in chess.com events, hand-added specials and team rosters you typed in. After each occurrence:

1. **Add the live rows to the training set.** Each compiled conflict is stored with its features, Δ, model version and outcome (played or not). These rows join the next refit, and they are the only source for features the PGN history lacks (Lichess tier, hand-added events).
2. **Write verified conflicts into history.** For every approved conflict on a past occurrence, insert into `attendance_conflicts` with a new `source = 'registry'` column (existing rows become `source = 'pgn'`). `build_attendance_conflicts.py` replaces only `pgn` rows. Conflict weeks you verified by hand then also leave the participation denominator.
3. **Monitor calibration live.** A section in `notebooks/model-performance-audit.ipynb`: realised attendance versus predicted, by Δ decile, market and format. Refit on a schedule (monthly, or after N new labelled rows), then compare the walk-forward score with the active version before promoting.

## 11. Relationship to the LLM researcher proposal

This changes what the LLM phases are *for*, for the better:

| Researcher proposal (v2) | With the registry |
|---|---|
| Per-market runs every few hours, each ±1 day around one event | One global lookahead; LLM runs on tournaments, not on markets |
| LLM output = per-market adjustment proposals (cut/nudge items) | LLM output = **entry proposals**: "player X is entered in tournament Y (source URL)", ratings, team format and schedule corrections. These are facts (model inputs), which a cheap model handles well. |
| Policy stage maps evidence categories to numbers by a hand-set table | The conflict model (§7) does this from data, once for all markets |
| Rejection memory table | `tournament_entries.status = rejected` already stores it, and re-collection respects it |
| `adjustment_proposals` + merge-apply + version check | Mostly unnecessary: entries are small, independent rows, and the compiled layer never writes `manual_adjustments` |

The emergency halt (§7 of that proposal) still makes sense, keyed to "an approved tournament gained a confirmed high-value entrant within N hours of an occurrence".

## 12. Failure modes and safeguards

| Risk | Safeguard |
|---|---|
| A false name match hits the wrong player | Only `exact` / `strong` auto-confirm; others are `probable` (half weight) until you look. Nothing compiles before the tournament is approved. |
| A schedule change after approval goes unnoticed | `schedule_changed` puts it back in *Needs review*; the compile hash change shows the staleness banner on every affected market. |
| An entrant withdraws | `in_source = 0` raises it in review; you set `withdrawn`. |
| A compiled Δ is wrong for one market | `ignore_conflict` or a manual override on that market's Attendance page, without touching the tournament. |
| The model extrapolates badly (a feature combination it never saw, e.g. a team event in Bullet Brawl) | Δ clipped to a range set from training (e.g. [−6, 0]); the preview shows the breakdown; a per-tournament `delta_override`; walk-forward scores per market before promotion. |
| Training/serving skew | One feature module (`src/conflicts/features.py`) for both; a test that rebuilds a few historical rows through the live path and compares. |
| A refit moves prices silently | Versions are promoted by hand; promotion shows the staleness banner (§8.3). |
| The lookahead fails | Advisory, like research today: logged on `lookahead_runs`, the pipeline continues, and existing approved data keeps compiling. |
| Lichess rate limits | Horizon is one week; details only for tournaments with a clash, plus re-checks of unfinished registry tournaments; sequential requests with a gap and one 429 backoff as now; `lichess_max_tours` cap logged. |

## 13. Phased roadmap

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Data + model v0 (offline)** | Keep PGN `Round`, Elo and team tags in `update_broadcasts.py` and backfill from 2023-01; team keyword list; dataset builder with walk-forward `p_base` for all three markets; v0 logistic model with offset; walk-forward scoring against the §7.4 baselines. | v0 beats the constant-per-timing-bucket baseline on the VIP cohort, with bootstrap ranges that exclude zero improvement. |
| **1. Registry, in shadow** | Migration; `tournament_entries`, `event_clashes`, review fields, team format; global lookahead (pipeline + daily) with re-checks and linking; Conflicts page with review, manual players, add-tournament with round lists; compiler running with v0 and **displayed on Attendance, but not applied**. | Two weeks in which the compiled Δs look sane next to what you'd have typed by hand, and differences have explanations. |
| **2. Apply** | `conflict` and `ignore_conflict` in `run_adjusted`; `conflict_applied` + staleness banner; cross-market recompute; model promotion; schedule overrides; retire the per-market Research section and the `policy` column. | A full week across all three markets with no conflict adjustments typed by hand. |
| **3. Close the loop** | Live rows into training; `attendance_conflicts.source = 'registry'`; live calibration monitoring and scheduled refits; v1 (boosted trees) comparison; opt-in pipeline auto-complete per market; T − 4 h lookahead. | A refit promoted on live plus historical data; auto-complete on for at least one market. |
| **4. LLM entry finder** | Researcher phase 2 re-pointed at filling entry lists, ratings and schedules (chess-results, federation pages), output as `source = 'llm'` probable entries with URLs. | Entry proposals you accept most of the time. |

## 14. Open questions

**Decided 2026-11-22** (1–5 in the first review, 6–8 in the second):

1. **Conflict effect:** learned log-odds Δ from conflict features, walk-forward, with the player's `p_participate` as offset (§7).
2. **Round granularity:** round position is a model feature; no per-round rates typed by hand.
3. **Performance:** out of scope. Only attendance is adjusted, including for Bullet Brawl.
4. **Global jobs:** reserved `market = '_global'` in the trade queue.
5. **Horizon:** discover up to each market's next event; store every found tournament in full; re-check unfinished registry tournaments every run; no horizon for hand-added tournaments.

6. **Training data:** 2023 onwards only, with mild recency weighting (starting at a one-year half-life, tuned by walk-forward) (§7.4).
7. **Per-player effect:** shrunk toward the population with Freestyle Friday's field-prior form, `k / (k + n_eff)`, starting at k = 3 (§7.4).
8. **Team format:** PGN or Lichess team flag, then a keyword list on the name, then your toggle (§7.3).

No open questions remain before phase 0.
