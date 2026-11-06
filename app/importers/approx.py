"""
Approximate tracks for races swum before the watch could record them.

    python -m importers.approx --check  [--geojson out.geojson]   # writes nothing
    python -m importers.approx --import [--dsn ...]

The races, their official times and the course landmarks live in
approx_swims.toml next to this file — read its header first. This module turns
each entry into a workout (provider 'approx') and a route: a polyline through
the landmarks, densified, with timestamps spread evenly over the finish time.

What is real and what is not, per row:
  * distance_m, duration_s, started_at's DATE  — the race's own figures
  * the hour of the start                      — the published schedule, or memory
  * every GPS fix                              — approximated
So the swim belongs on the map and in the per-year volume; the shape of the
track carries no pacing information. workout_routes.source_file is 'approx'
and workouts.raw keeps the whole TOML entry, sources included.

Idempotent: ids are 'approx:<slug>', every write is an upsert, and a re-import
replaces the track. A real track of the same session from another provider
supersedes the approximation (provider_priority ranks 'approx' last).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tomllib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from canon import local_day
from importers.routes import haversine
from settings import DSN, TZ

PROVIDER = "approx"
DATA = Path(__file__).with_name("approx_swims.toml")
STEP_M = 15.0          # spacing of the generated fixes
EARTH_M = 6_371_000.0


def load(path: Path = DATA) -> list[dict]:
    return tomllib.loads(path.read_text())["swim"]


def destination(lat: float, lon: float, bearing_deg: float, metres: float) -> tuple[float, float]:
    """The point `metres` away on a great circle at `bearing_deg` (0 = north)."""
    d, b = metres / EARTH_M, math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def geometry(entry: dict) -> list[tuple[float, float]]:
    """The course as landmarks, before densifying."""
    if "legs" in entry:
        pts = [tuple(entry["start"])]
        for bearing, metres in entry["legs"]:
            pts.append(destination(*pts[-1], bearing, metres))
        if entry.get("close"):
            pts.append(pts[0])
    else:
        pts = [tuple(p) for p in entry["waypoints"]]
    laps = int(entry.get("laps", 1))
    if laps > 1:
        loop = pts[1:] if pts[0] == pts[-1] else pts
        pts = pts + loop * (laps - 1)
    return pts


def length_m(pts: list[tuple[float, float]]) -> float:
    return sum(haversine(a, b) for a, b in zip(pts, pts[1:]))


def densify(pts: list[tuple[float, float]], step: float = STEP_M) -> list[tuple[float, float]]:
    """Linear interpolation so the map draws a track, not a few long jumps."""
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(haversine(a, b) // step))
        out += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
                for i in range(1, n + 1)]
    return out


def seconds(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def start_of(entry: dict) -> datetime:
    hh, mm = (int(x) for x in entry["start_local"].split(":"))
    return datetime.combine(date.fromisoformat(entry["date"]), time(hh, mm),
                            tzinfo=ZoneInfo(entry["tz"]))


def track(entry: dict) -> list[tuple]:
    """(ts, lat, lon, ele_m, speed_ms) — the routes.write_route shape."""
    pts = densify(geometry(entry))
    total = length_m(pts)
    dur = seconds(entry["duration"])
    start = start_of(entry)
    speed = total / dur if dur else None
    out, done = [], 0.0
    for i, p in enumerate(pts):
        if i:
            done += haversine(pts[i - 1], p)
        out.append((start + timedelta(seconds=dur * done / total if total else 0),
                    p[0], p[1], None, speed))
    return out


COLS = ("id", "provider", "external_id", "sport", "is_indoor", "name_raw", "location_raw",
        "started_at", "ended_at", "local_day", "duration_s", "distance_m",
        "moving_s", "pace_s_per_100m", "tz_name", "tz_source", "local_started_at", "raw")


def workout_row(entry: dict, tz: str = TZ) -> tuple:
    start = start_of(entry)
    dur = seconds(entry["duration"])
    dist = float(entry["distance_m"])
    return (
        f"{PROVIDER}:{entry['slug']}", PROVIDER, entry["slug"], "swim", False,
        entry["name"], None,
        start, start + timedelta(seconds=dur), local_day(start, tz),
        dur, dist,
        # A race is swum without stops at a wall: elapsed is active time.
        dur, dur / (dist / 100.0),
        # The zone is known exactly — it is where the race was. 'manual' outranks
        # every inferred source in localtime.
        entry["tz"], "manual", start.replace(tzinfo=None),
        json.dumps({**entry, "approximate_track": True}),
    )


async def run_import(dsn: str, entries: list[dict]) -> dict:
    import psycopg
    from db import BatchWriter
    from derive import mark_superseded
    from importers.routes import route_writers, write_route

    counts = {"workouts": 0, "route_points": 0}
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        wk = BatchWriter(conn, "workouts", COLS, conflict=("id",))
        for e in entries:
            await wk.add(workout_row(e))
            counts["workouts"] += 1
        await wk.flush()
        pts, routes = route_writers(conn)
        for e in entries:
            counts["route_points"] += await write_route(
                conn, pts, routes, f"{PROVIDER}:{e['slug']}", "approx", track(e))
        await pts.flush()
        await routes.flush()
        counts["superseded_changed"] = await mark_superseded(conn)
        await conn.commit()
    return counts


def check(entries: list[dict], geojson: Path | None) -> None:
    print(f"{'slug':<32} {'date':<10} {'track km':>8} {'race km':>8} {'time':>8}  source")
    feats = []
    for e in entries:
        t = track(e)
        km = length_m([(p[1], p[2]) for p in t]) / 1000
        print(f"{e['slug']:<32} {e['date']:<10} {km:>8.2f} {e['distance_m']/1000:>8.2f} "
              f"{e['duration']:>8}  {e['time_source']}")
        feats.append({"type": "Feature",
                      "properties": {k: e[k] for k in ("slug", "name", "date", "duration",
                                                       "time_source")},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[p[2], p[1]] for p in t]}})
    if geojson:
        geojson.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
        print(f"\nGeoJSON for review: {geojson}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Approximate tracks for unrecorded races.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="print and optionally export; no DB")
    g.add_argument("--import", dest="do_import", action="store_true")
    ap.add_argument("--geojson", type=Path)
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args(argv)
    entries = load(args.data)
    if args.check:
        check(entries, args.geojson)
    else:
        print(asyncio.run(run_import(args.dsn, entries)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
