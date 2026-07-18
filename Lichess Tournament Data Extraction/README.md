# Lichess broadcast → tournament attendance pipeline

Turns the Lichess open broadcast database (all officially relayed tournaments,
CC0-adjacent licensing, monthly `.pgn.zst` dumps) into tidy tables for modeling
Titled Tuesday attendance.

## Run

```bash
pip install zstandard

# 1. Download monthly dumps (Sep 2025 → present ≈ 11 files)
python fetch_broadcasts.py --start 2025-09 --end 2026-07 --out ./dumps

# 2. Parse into tables (header-only, streaming; a full month parses in seconds)
python parse_broadcasts.py dumps/*.pgn.zst --out ./tables
```

The full index of available files: https://database.lichess.org/broadcast/list.txt
(files are `lichess_db_broadcast_YYYY-MM.pgn.zst`; the current month is only
published after it ends — for in-progress months use the live broadcast API,
`GET https://lichess.org/api/broadcast`, which exposes tour dates, time control,
and round schedules for ongoing events).

## Outputs

| file | grain | use |
|---|---|---|
| `appearances.csv` | (player, game) | the attendance table; join to FIDE ID |
| `games.csv` | game | raw per-game record with all headers |
| `events.csv` | broadcast tournament | date span, #rounds, #players, modal time control |
| `rounds.csv` | (tournament, round) | earliest & median game start (UTC) |

## Modeling notes

- **Titled Tuesday runs 11:00 AM Eastern** = **15:00 UTC** during EDT
  (roughly Mar–Nov) and **16:00 UTC** during EST. Compare against
  `rounds.csv.median_start_utc`. A classical round starting 5–10h before TT can
  still collide (90+30 games routinely run 4–5 hours); a same-day round
  *starting* within ~5h before or ~4h after 15:00 UTC is a hard conflict.
- `UTCTime` is when the broadcast game record went live, which for relayed OTB
  games tracks the actual round start within minutes. Use the **median** per
  round rather than the earliest (stray early boards / test games exist).
- `BroadcastName` is the cleanest event key ("Tournament | Section"). `Event`
  is sometimes messier.
- Dedupe caveat: occasionally an event is relayed twice (official + community
  relay). Dedupe on (player pair, UTCDate, round) if you see doubled games.
- FIDE IDs are present on most (not all) players; fall back to normalized
  "Last, First" name matching against the FIDE rating list when missing.
