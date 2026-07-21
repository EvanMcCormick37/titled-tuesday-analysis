#!/usr/bin/env python3
"""Parse TWIC weekly zips into OTB tournament metadata, schedule & attendance tables.

Reads PGN headers only. Merges events that span multiple weekly issues.

Key conventions exploited (TWIC-specific):
  * Site tag: OTB events carry "City CCC" (3-letter country); internet events
    are tagged "... INT" (e.g. "Chess.com INT", "lichess.org INT", "Tornelo INT").
  * EventType tag (when present): "tourn", "swiss", "k.o.", "team", ... with
    speed suffixes like "(rapid)"/"(blitz)".
  * WhiteElo/BlackElo: FIDE (standard-list) ratings as published by TWIC.
  * Per-round Date tags give the day each round was played.

Outputs:
  events.csv       one row per tournament (metadata + prestige markers)
  appearances.csv  one row per (player, tournament)
  rounds.csv       one row per (tournament, round) with play date(s)

Usage:
    python parse_twic.py twic_zips/*.zip --out ./twic_tables
    python parse_twic.py twic_zips/*.zip --out ./twic_tables --include-online
"""
import argparse
import csv
import io
import re
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path

INT_SITE = re.compile(r"\bINT\b", re.I)
COUNTRY = re.compile(r"\b([A-Z]{3})\s*$")
SPEED_RE = re.compile(r"\b(blitz|rapid|bullet|armageddon|titled tue|arena)\b", re.I)

# Prestige tier 1: hand-curated elite-circuit patterns (matched on event name).
TIER1 = re.compile(
    r"candidates|olympiad|world\s+(chess\s+)?ch|world cup|grand swiss|"
    r"fide grand prix|sinquefield|norway chess|tata steel masters|"
    r"wijk aan zee.*masters|grenke.*classic|superbet|american cup|"
    r"grand chess tour|shamkir|gashimov|dortmund|biel.*(gmt|masters)|"
    r"us championship|london chess classic", re.I)


def iter_games_from_zip(zpath: Path):
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if not name.lower().endswith(".pgn"):
                continue
            with z.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", errors="replace")
                tags, in_movetext = {}, False
                for line in text:
                    line = line.strip()
                    if line.startswith("[") and line.endswith("]") and '"' in line:
                        if in_movetext and tags:
                            yield tags
                            tags, in_movetext = {}, False
                        try:
                            key, rest = line[1:-1].split(" ", 1)
                            tags[key] = rest.strip().strip('"')
                        except ValueError:
                            pass
                    elif line and tags:
                        in_movetext = True
                if tags:
                    yield tags


def norm_date(s):
    """'2026.07.16' -> '2026-07-16'; tolerate '????.??.??' partials."""
    if not s or "?" in s.split(".")[0]:
        return ""
    parts = (s.replace("?", "01").split("."))
    try:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except (ValueError, IndexError):
        return ""


def event_key(tags):
    ev = tags.get("Event", "").strip()
    site = tags.get("Site", "").strip()
    year = (norm_date(tags.get("EventDate", "")) or norm_date(tags.get("Date", "")))[:4]
    return f"{ev.lower()}|{site.lower()}|{year}"


def speed_class(tags):
    text = f'{tags.get("Event","")} {tags.get("EventType","")}'
    m = SPEED_RE.search(text)
    if not m:
        return "classical"
    w = m.group(1).lower()
    return {"titled tue": "blitz", "arena": "online-arena"}.get(w, w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="twic*.zip files")
    ap.add_argument("--out", default="./twic_tables")
    ap.add_argument("--include-online", action="store_true",
                    help="keep Site='... INT' events (excluded by default)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ev = {}
    players = defaultdict(dict)          # key -> {player: {...}}
    rounds = defaultdict(set)            # (key, round) -> {dates}
    n_games = n_skipped_online = 0

    for zpath in sorted(map(Path, args.inputs)):
        issue = re.sub(r"\D", "", zpath.stem) or zpath.stem
        for tags in iter_games_from_zip(zpath):
            site = tags.get("Site", "").strip()
            is_otb = not INT_SITE.search(site)
            if not is_otb and not args.include_online:
                n_skipped_online += 1
                continue
            n_games += 1
            key = event_key(tags)
            if key not in ev:
                cm = COUNTRY.search(site)
                ev[key] = {"event": tags.get("Event", "").strip(), "site": site,
                           "country": cm.group(1) if (cm and is_otb) else "",
                           "is_otb": is_otb, "speed": speed_class(tags),
                           "event_type": tags.get("EventType", ""),
                           "event_date": norm_date(tags.get("EventDate", "")),
                           "first": "", "last": "", "n_games": 0,
                           "issues": set()}
            e = ev[key]
            e["n_games"] += 1
            e["issues"].add(issue)
            if not e["event_type"] and tags.get("EventType"):
                e["event_type"] = tags["EventType"]
            d = norm_date(tags.get("Date", ""))
            if d:
                e["first"] = min(e["first"] or d, d)
                e["last"] = max(e["last"] or d, d)
                rnd = tags.get("Round", "").split(".")[0]
                rounds[(key, rnd)].add(d)
            for color in ("White", "Black"):
                name = tags.get(color, "").strip()
                if not name or name.lower() in ("nn", "bye"):
                    continue
                p = players[key].setdefault(name, {"elo": "", "fide_id": "", "title": "",
                                                   "n_games": 0})
                p["n_games"] += 1
                elo = tags.get(f"{color}Elo", "")
                if elo.isdigit():
                    p["elo"] = max(int(p["elo"] or 0), int(elo))
                p["fide_id"] = p["fide_id"] or tags.get(f"{color}FideId", "")
                p["title"] = p["title"] or tags.get(f"{color}Title", "")

    # ---- events.csv with prestige markers ----
    with open(out / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_key", "event", "site", "country", "is_otb", "speed",
                    "event_type", "rating_system", "first_date", "last_date",
                    "n_days", "n_rounds", "n_games", "n_players",
                    "mean_elo", "top10_mean_elo", "max_elo", "n_2700", "n_2600",
                    "prestige_tier", "twic_issues"])
        for key, e in sorted(ev.items(), key=lambda kv: kv[1]["first"]):
            elos = sorted((p["elo"] for p in players[key].values() if p["elo"]),
                          reverse=True)
            top10 = elos[:10]
            n_r = len({r for (k, r) in rounds if k == key})
            try:
                n_days = ((__import__("datetime").date.fromisoformat(e["last"])
                           - __import__("datetime").date.fromisoformat(e["first"])).days + 1) \
                    if e["first"] and e["last"] else ""
            except ValueError:
                n_days = ""
            tier = (1 if TIER1.search(e["event"]) else
                    2 if top10 and statistics.mean(top10) >= 2700 else
                    3 if top10 and statistics.mean(top10) >= 2550 else 4)
            w.writerow([key, e["event"], e["site"], e["country"], e["is_otb"],
                        e["speed"], e["event_type"],
                        "FIDE" if e["is_otb"] else "FIDE/site",
                        e["first"], e["last"], n_days, n_r, e["n_games"],
                        len(players[key]),
                        round(statistics.mean(elos), 1) if elos else "",
                        round(statistics.mean(top10), 1) if top10 else "",
                        elos[0] if elos else "",
                        sum(x >= 2700 for x in elos), sum(x >= 2600 for x in elos),
                        tier, ";".join(sorted(e["issues"]))])

    # ---- appearances.csv ----
    with open(out / "appearances.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["player", "fide_id", "title", "elo", "n_games",
                    "event_key", "event", "first_date", "last_date", "is_otb", "speed"])
        for key, ps in players.items():
            e = ev[key]
            for name, p in ps.items():
                w.writerow([name, p["fide_id"], p["title"], p["elo"], p["n_games"],
                            key, e["event"], e["first"], e["last"],
                            e["is_otb"], e["speed"]])

    # ---- rounds.csv ----
    with open(out / "rounds.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_key", "round", "dates"])
        for (key, rnd), dates in sorted(rounds.items()):
            w.writerow([key, rnd, ";".join(sorted(dates))])

    print(f"{len(ev)} events, {n_games} games kept, "
          f"{n_skipped_online} online games skipped -> {out}/")


if __name__ == "__main__":
    main()
