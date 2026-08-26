"""
GPX route importer.

    docker exec addon_local_longcourse \
        python -m importers.routes /share/archive/apple_health_export/export.xml
    # or from a laptop against the LAN database:
    python -m importers.routes ~/Downloads/apple_health_export/export.xml --dsn ...

The native archive makes route matching trivial. Each <Workout> carries a
<WorkoutRoute><FileReference path="/workout-routes/route_*.gpx"/>, so a route is
tied to its workout directly — none of the localised-filename guessing the
HAE-era design in CLAUDE.md worried about. This reads export.xml only to learn
each workout's route file, resolves the workout id from the database (by start
time, same as the metric import), then parses the GPX itself.

Geometry lands in route_points at full fidelity (a long run is ~20k fixes); the
dashboards thin it in the query. workout_routes gets one summary row per route:
point count, GPS distance, bounding box and centre for the map.

Idempotent: every write is an upsert keyed on (workout_id, seq), so re-running
replaces rather than duplicates.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path
from xml.etree.ElementTree import iterparse

import psycopg

from db import BatchWriter
from parse import as_float, parse_dt
from settings import DSN

POINT_COLS = ("workout_id", "seq", "ts", "lat", "lon", "ele_m", "speed_ms")
ROUTE_COLS = ("workout_id", "source_file", "point_count", "started_at", "ended_at",
              "distance_m", "min_lat", "max_lat", "min_lon", "max_lon",
              "center_lat", "center_lon")


def _local(tag: str) -> str:
    """Strip the XML namespace: '{http://…}trkpt' -> 'trkpt'."""
    return tag.rsplit("}", 1)[-1]


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle metres between two (lat, lon) points."""
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def workout_routes_from_export(path: Path) -> dict[int, str]:
    """Map workout start-time epoch -> GPX filename, read from export.xml.

    The FileReference lives inside the <Workout>, so the route is associated
    with the workout that contains it; we key on the workout's start time to
    look its id up in the database afterwards.
    """
    out: dict[int, str] = {}
    ctx = iterparse(str(path), events=("start", "end"))
    _, root = next(ctx)
    for event, elem in ctx:
        if event != "end" or _local(elem.tag) != "Workout":
            continue
        start = parse_dt(elem.get("startDate"))
        ref = None
        for wr in elem.iter():
            if _local(wr.tag) == "FileReference" and wr.get("path"):
                ref = wr.get("path")
                break
        if start and ref:
            out[int(start.timestamp())] = Path(ref).name
        elem.clear()
        root.clear()
    return out


def parse_gpx(path: Path):
    """Yield (ts, lat, lon, ele, speed) per track point, streaming.

    Namespace-agnostic: Apple writes the GPX/1.1 namespace, but matching on the
    local tag name keeps this working if that ever changes.
    """
    ctx = iterparse(str(path), events=("end",))
    for _, elem in ctx:
        if _local(elem.tag) != "trkpt":
            continue
        lat, lon = as_float(elem.get("lat")), as_float(elem.get("lon"))
        if lat is None or lon is None:
            elem.clear()
            continue
        ts = ele = speed = None
        for ch in elem:
            t = _local(ch.tag)
            if t == "time":
                ts = parse_dt(ch.text)
            elif t == "ele":
                ele = as_float(ch.text)
            elif t == "extensions":
                for ex in ch:
                    if _local(ex.tag) == "speed":
                        speed = as_float(ex.text)
        yield ts, lat, lon, ele, speed
        elem.clear()


async def import_routes(export: Path, routes_dir: Path, dsn: str) -> dict:
    print(f"reading route references from {export.name} …", flush=True)
    refs = workout_routes_from_export(export)
    print(f"  {len(refs)} workouts reference a GPX file", flush=True)

    stats = {"routes": 0, "points": 0, "missing_file": 0, "unmatched": 0, "empty": 0}

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cur = await conn.execute("SELECT id, started_at FROM workouts")
        by_start = {int(ts.timestamp()): wid for wid, ts in await cur.fetchall()}

        pts = BatchWriter(conn, "route_points", POINT_COLS,
                          conflict=("workout_id", "seq"))
        routes = BatchWriter(conn, "workout_routes", ROUTE_COLS,
                             conflict=("workout_id",),
                             update=[c for c in ROUTE_COLS if c != "workout_id"])

        for epoch, fname in sorted(refs.items()):
            wid = by_start.get(epoch)
            if wid is None:
                stats["unmatched"] += 1
                continue
            gpx = routes_dir / fname
            if not gpx.exists():
                stats["missing_file"] += 1
                continue

            seq = 0
            prev = None
            dist = 0.0
            lo_lat = lo_lon = math.inf
            hi_lat = hi_lon = -math.inf
            first_ts = last_ts = None
            sum_lat = sum_lon = 0.0

            for ts, lat, lon, ele, speed in parse_gpx(gpx):
                await pts.add((wid, seq, ts, lat, lon, ele, speed))
                if prev is not None:
                    dist += haversine(prev, (lat, lon))
                prev = (lat, lon)
                lo_lat, hi_lat = min(lo_lat, lat), max(hi_lat, lat)
                lo_lon, hi_lon = min(lo_lon, lon), max(hi_lon, lon)
                sum_lat += lat
                sum_lon += lon
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                seq += 1

            if seq == 0:
                stats["empty"] += 1
                continue

            await routes.add((
                wid, fname, seq, first_ts, last_ts, round(dist, 1),
                lo_lat, hi_lat, lo_lon, hi_lon,
                sum_lat / seq, sum_lon / seq,
            ))
            stats["routes"] += 1
            stats["points"] += seq
            if stats["routes"] % 50 == 0:
                await pts.flush()
                await routes.flush()
                await conn.commit()
                print(f"  … {stats['routes']} routes, {stats['points']:,} points",
                      flush=True)

        await pts.flush()
        await routes.flush()
        await conn.commit()

    print(f"\ndone: {stats}")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import GPX workout routes.")
    ap.add_argument("export", type=Path, help="path to export.xml")
    ap.add_argument("--routes-dir", type=Path, default=None,
                    help="folder of route_*.gpx (default: <export dir>/workout-routes)")
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args(argv)

    if not args.export.exists():
        print(f"no such file: {args.export}", file=sys.stderr)
        return 1
    routes_dir = args.routes_dir or args.export.parent / "workout-routes"
    if not routes_dir.is_dir():
        print(f"routes folder not found: {routes_dir}", file=sys.stderr)
        return 1

    asyncio.run(import_routes(args.export, routes_dir, args.dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
