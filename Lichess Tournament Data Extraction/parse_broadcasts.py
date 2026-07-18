#!/usr/bin/env python3
"""Parse Lichess broadcast .pgn.zst dumps into tidy attendance & schedule tables.

Reads headers only (never parses movetext), streaming, so a full month
(~hundreds of MB decompressed) parses in seconds with tiny memory use.

Outputs (CSV):
  games.csv        one row per broadcast game
  appearances.csv  long format: one row per (player, game) -- the attendance table
  events.csv       one row per broadcast tournament: date span, rounds, players, modal TC
  rounds.csv       one row per (event, round): earliest/median game start (UTC)

Usage:
    python parse_broadcasts.py dumps/*.pgn.zst --out ./tables
"""
import argparse
import csv
import io
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import zstandard

GAME_FIELDS = [
    "broadcast_name", "event", "round", "board",
    "white", "black", "white_title", "black_title",
    "white_elo", "black_elo", "white_fide_id", "black_fide_id",
    "time_control", "utc_datetime", "result", "game_url",
]


def iter_games(path: Path):
    """Yield one dict of PGN tag headers per game, streaming."""
    dctx = zstandard.ZstdDecompressor()
    opener = (lambda p: dctx.stream_reader(open(p, "rb"))) if path.suffix == ".zst" \
        else (lambda p: open(p, "rb"))
    with opener(path) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        tags, in_movetext = {}, False
        for line in text:
            line = line.strip()
            if line.startswith("[") and line.endswith("]") and '"' in line:
                if in_movetext and tags:      # previous game's movetext ended
                    yield tags
                    tags, in_movetext = {}, False
                try:
                    key, rest = line[1:-1].split(" ", 1)
                    tags[key] = rest.strip().strip('"')
                except ValueError:
                    pass
            elif line and tags:
                in_movetext = True            # movetext (or result) line
        if tags:
            yield tags


def to_utc(tags):
    d, t = tags.get("UTCDate"), tags.get("UTCTime")
    if not d:
        return None
    try:
        return datetime.strptime(f"{d} {t or '00:00:00'}", "%Y.%m.%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def row_from(tags):
    dt = to_utc(tags)
    return {
        "broadcast_name": tags.get("BroadcastName", ""),
        "event": tags.get("Event", ""),
        "round": tags.get("Round", ""),
        "board": tags.get("Board", ""),
        "white": tags.get("White", ""),
        "black": tags.get("Black", ""),
        "white_title": tags.get("WhiteTitle", ""),
        "black_title": tags.get("BlackTitle", ""),
        "white_elo": tags.get("WhiteElo", ""),
        "black_elo": tags.get("BlackElo", ""),
        "white_fide_id": tags.get("WhiteFideId", ""),
        "black_fide_id": tags.get("BlackFideId", ""),
        "time_control": tags.get("TimeControl", ""),
        "utc_datetime": dt.isoformat() if dt else "",
        "result": tags.get("Result", ""),
        "game_url": tags.get("GameURL", ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help=".pgn.zst (or .pgn) files")
    ap.add_argument("--out", default="./tables")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    games_f = open(out / "games.csv", "w", newline="", encoding="utf-8")
    app_f = open(out / "appearances.csv", "w", newline="", encoding="utf-8")
    games_w = csv.DictWriter(games_f, fieldnames=GAME_FIELDS)
    games_w.writeheader()
    app_w = csv.writer(app_f)
    app_w.writerow(["player", "fide_id", "title", "elo", "color",
                    "broadcast_name", "event", "round", "time_control",
                    "utc_datetime", "game_url"])

    ev = defaultdict(lambda: {"players": set(), "rounds": set(), "tcs": Counter(),
                              "first": None, "last": None, "n_games": 0})
    rnd = defaultdict(list)   # (broadcast_name, round) -> [datetimes]
    n = 0
    for p in map(Path, args.inputs):
        print(f"parsing {p.name} ...")
        for tags in iter_games(p):
            r = row_from(tags)
            games_w.writerow(r)
            for color in ("white", "black"):
                app_w.writerow([r[color], r[f"{color}_fide_id"], r[f"{color}_title"],
                                r[f"{color}_elo"], color, r["broadcast_name"],
                                r["event"], r["round"], r["time_control"],
                                r["utc_datetime"], r["game_url"]])
            key = r["broadcast_name"] or r["event"]
            e = ev[key]
            e["n_games"] += 1
            e["players"].update(x for x in (r["white"], r["black"]) if x)
            if r["round"]:
                e["rounds"].add(r["round"])
            if r["time_control"]:
                e["tcs"][r["time_control"]] += 1
            dt = to_utc(tags)
            if dt:
                e["first"] = min(e["first"] or dt, dt)
                e["last"] = max(e["last"] or dt, dt)
                rnd[(key, r["round"])].append(dt)
            n += 1
    games_f.close()
    app_f.close()

    with open(out / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["broadcast_name", "first_game_utc", "last_game_utc",
                    "n_rounds", "n_games", "n_players", "modal_time_control"])
        for k, e in sorted(ev.items()):
            w.writerow([k,
                        e["first"].isoformat() if e["first"] else "",
                        e["last"].isoformat() if e["last"] else "",
                        len(e["rounds"]), e["n_games"], len(e["players"]),
                        e["tcs"].most_common(1)[0][0] if e["tcs"] else ""])

    with open(out / "rounds.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["broadcast_name", "round", "n_games",
                    "earliest_start_utc", "median_start_utc"])
        for (k, r), dts in sorted(rnd.items()):
            dts.sort()
            med = dts[len(dts) // 2]
            w.writerow([k, r, len(dts), dts[0].isoformat(), med.isoformat()])

    print(f"parsed {n} games across {len(ev)} broadcast tournaments -> {out}/")


if __name__ == "__main__":
    main()
