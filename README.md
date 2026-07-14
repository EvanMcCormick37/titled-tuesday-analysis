# Titled Tuesday Results Database

Compiled July 3, 2026 from the community-maintained archive at
https://github.com/cmgchess/Titled-Tuesday-Data (Unlicense / public domain),
which in turn scrapes Chess.com tournament pages and the Wayback Machine.

## Files

### titled_tuesday_tournaments.csv — 598 events, Oct 28, 2014 → Jun 30, 2026
One row per event:
- date (YYYY-MM-DD), time_local (as listed on the source page, mixed timezones — treat as approximate)
- title, session ("early"/"late" for the double-tournament era Feb 2022–Aug 2025, blank otherwise)
- num_players, winner, slug, url (chess.com tournament page)

### titled_tuesday_standings.csv — 285,147 player-results
Full final standings for every event, one row per player per tournament:
- date, tournament_slug, session
- rank, username, title (GM/IM/FM/...), country, rating (chess.com blitz rating at event time)
- score, tie_break, wins, draws, byes

### update_titled_tuesday.py
Script to append future events using Chess.com's free Published-Data API
(https://api.chess.com/pub/tournament/{slug}). Requires `requests` and
`beautifulsoup4`. Note the API's final-round standings include points and
tie-break but NOT country/title/rating — those need a join against
/pub/player/{username} per player if you want to extend those columns.

## Sanity check
Winner counts since Oct 20, 2020 computed from this data — Hikaru 93,
MagnusCarlsen 45 — exactly match Chess.com's officially published figures
(chess.com/article/view/titled-tuesday), so the archive appears complete
and accurate for the modern era.

## Known caveats
1. **"Russia" country mapping**: from March 2022 Chess.com replaced the
   Russian flag tooltip with the text "Click here to see our stance on the
   war in Ukraine". 26,671 standings rows carried that string in the country
   field; they have been normalized to "Russia". Some players later changed
   their listed federation, so country reflects what the profile showed at
   scrape time.
2. **Format changes across eras** (worth segmenting your analysis by):
   - Dec 2014–2019: monthly-ish 3+2 blitz events, sometimes two sessions per day
   - Apr 2020: became weekly; Oct 20, 2020: expanded to 11 rounds
   - Feb 1, 2022–Aug 26, 2025: two tournaments weekly (early + late)
   - Sep 2, 2025–present: single weekly event, time control changed 3+1 → 5+0
3. A few 2016 events are marked winner="Cancelled".
4. Ratings are Chess.com blitz ratings, not FIDE.
5. Wins/draws/byes columns are occasionally 0/blank for very old events.

## Other resources found during research
- Per-game PGNs (every move of every game): the PubAPI games endpoints, or the
  R package https://github.com/IgorRigolon/titled.tuesday which has raw PGNs
- Kaggle dataset (older snapshot): kaggle.com/datasets/garyongguanjie/chess-com-titled-tuesday-dataset
- Winner/stats dashboards: https://www.titled-tuesday.com/
- PubAPI docs: https://www.chess.com/announcements/view/published-data-api
