# chess.com/events is entered by hand, not scraped

2026-11-20 · decided with the user

**Decision.** The attendance researcher's automated tournament calendar is the Lichess broadcast API. chess.com events are added by hand on the dashboard. No browser automation, stealth plugins or challenge solvers.

**Why.** chess.com/events is an HTML shell whose event list is drawn by a client-side script, and plain HTTP requests for that script get a Cloudflare challenge. A headless Chromium probe (`scripts/chesscom_events_probe.py`) from a residential connection got HTTP 403 with `cf-mitigated: challenge` on the page itself. A Railway datacenter IP would be treated at least as strictly, so that test was skipped. Getting past it would mean disguising the automation, which the user ruled out, and it would break whenever Cloudflare changed its rules. `api.chess.com`, which the standings scrapers use, isn't affected.

**What would change it.** chess.com offering sanctioned access (an API key or a feed).
