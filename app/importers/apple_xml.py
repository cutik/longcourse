"""
Native Apple Health export.xml importer.

    docker exec addon_local_longcourse \
        python -m importers.apple_xml --scan   /share/archive/export.xml
    docker exec addon_local_longcourse \
        python -m importers.apple_xml --import /share/archive/export.xml

Why this exists alongside the HAE push path: Health Auto Export is the only way
to get *automatic* incremental data (it holds HealthKit background delivery and
posts on a schedule), but it cannot practically hand over years of history —
Cloudflare caps bodies at 100 MB and a whole-history JSON does not fit in a Pi's
RAM. Apple's own "Export All Health Data" produces the complete archive but is a
manual, share-sheet-only action that no API or Shortcut can trigger. So: xml for
the past, HAE for the present. Neither replaces the other.

Memory: the file runs to hundreds of megabytes and is parsed with iterparse,
clearing each element and the accumulated root children as it goes, so footprint
is flat regardless of file size. Nothing is ever read whole.

Raw archiving: unlike an HAE push, the body is NOT inlined into raw_payloads —
a jsonb value tops out near 255 MB. The file stays on the mapped /share and is
registered in raw_files by digest, so a replay still has its source.

--scan writes nothing. Use it first: it reports which record types the archive
actually contains, how far back it goes, and whether swim workouts carry
HKWorkoutEventTypeLap events. That last one decides whether SWOLF can become a
measurement instead of the top-quartile estimate CLAUDE.md describes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from xml.etree.ElementTree import iterparse

from canon import (CANON, apple_sport, canonical, local_day, night_day,
                   sleep_stage, to_canonical_value)
from db import BatchWriter, register_raw_file, upsert_metric_meta
from parse import (as_float, dist_to_meters, duration_seconds, energy_kcal,
                   parse_dt, pool_length_from_text)
from settings import DSN, TZ

PROVIDER = "apple"
# Progress is printed every this many top-level elements. A full archive runs to
# millions of records and several minutes even on a fast machine; silence for
# that long is indistinguishable from a hang.
PROGRESS_EVERY = 250_000
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

OBS_COLS = ("provider", "metric", "ts", "local_day", "value", "unit", "source")
SLEEP_COLS = ("provider", "started_at", "ended_at", "stage", "source", "local_day")
LAP_COLS = ("workout_id", "idx", "started_at", "duration_s", "distance_m", "stroke_style")
WORKOUT_COLS = (
    "id", "provider", "external_id", "sport", "is_indoor", "name_raw", "location_raw",
    "started_at", "ended_at", "local_day", "duration_s", "distance_m",
    "active_kcal", "total_kcal", "avg_hr", "min_hr", "max_hr",
    "pool_length_m", "stroke_count", "raw",
)

# Distance statistics, in the order we would rather have them for a workout.
_DIST_STATS = (
    "HKQuantityTypeIdentifierDistanceSwimming",
    "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "HKQuantityTypeIdentifierDistanceCycling",
)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


TOP_LEVEL = ("Record", "Workout", "ActivitySummary")


def _elements(path: Path, tags: tuple[str, ...] = TOP_LEVEL):
    """Yield completed top-level elements, keeping memory flat.

    Two things matter here and both were got wrong first time round:

    Only top-level tags are yielded. A child's `end` event fires before its
    parent's, so yielding (and clearing) every end event empties a <Workout>'s
    MetadataEntry, WorkoutEvent and WorkoutStatistics children before the
    workout itself is ever handed over — silently dropping lap splits, pool
    length and heart-rate statistics while the import still reports success.

    Clearing the element is not enough on its own, either: the parser keeps
    every processed sibling hanging off the root, so a multi-million-record file
    grows without bound. Clearing root after each completed element is what
    actually caps memory.
    """
    ctx = iterparse(str(path), events=("start", "end"))
    _, root = next(ctx)
    for event, elem in ctx:
        if event != "end" or elem.tag not in tags:
            continue
        yield elem
        elem.clear()
        root.clear()


# ------------------------------------------------------------------ workout --

def _workout_fields(elem) -> dict:
    """Flatten one <Workout> into the columns `workouts` expects.

    Apple moved totals out of attributes and into <WorkoutStatistics> in later
    iOS versions without removing the old form, so both are read and the
    attribute wins when present.
    """
    meta = {m.get("key"): m.get("value")
            for m in elem.findall("MetadataEntry") if m.get("key")}
    stats = {s.get("type"): s for s in elem.findall("WorkoutStatistics") if s.get("type")}

    def m2(*keys):
        """Read a metadata value under any of its aliases.

        The real export.xml uses short keys — HKLapLength, HKIndoorWorkout,
        HKSwimmingLocationType — while HealthKit's API constants (and Apple's
        own documentation) spell them HKMetadataKey*. An earlier version keyed on
        the documented long form only and silently read NULL for every workout,
        wiping pool length and indoor/outdoor on the whole history.
        """
        for k in keys:
            if k in meta:
                return meta[k]
        return None

    activity = elem.get("workoutActivityType")
    sport = apple_sport(activity)

    distance = dist_to_meters(elem.get("totalDistance"), elem.get("totalDistanceUnit"))
    if distance is None:
        for t in _DIST_STATS:
            st = stats.get(t)
            if st is not None:
                distance = dist_to_meters(st.get("sum"), st.get("unit"))
                if distance is not None:
                    break

    energy = energy_kcal(elem.get("totalEnergyBurned"), elem.get("totalEnergyBurnedUnit"))
    if energy is None:
        st = stats.get("HKQuantityTypeIdentifierActiveEnergyBurned")
        if st is not None:
            energy = energy_kcal(st.get("sum"), st.get("unit"))

    hr = stats.get("HKQuantityTypeIdentifierHeartRate")
    strokes_stat = stats.get("HKQuantityTypeIdentifierSwimmingStrokeCount")

    # Indoor is a boolean in metadata; swimming additionally carries a location
    # type where 1 is a pool and 2 is open water. Prefer the swim-specific one —
    # an open-water swim can still be flagged indoor=0 by a watch that lost GPS.
    indoor_raw = m2("HKIndoorWorkout", "HKMetadataKeyIndoorWorkout")
    is_indoor = None
    if indoor_raw is not None:
        is_indoor = indoor_raw in ("1", "true", "YES")
    loc = m2("HKSwimmingLocationType", "HKMetadataKeySwimmingLocationType")
    if loc in ("1", "2"):
        is_indoor = loc == "1"

    return {
        "activity": activity,
        "sport": sport,
        "is_indoor": is_indoor,
        "source": elem.get("sourceName"),
        "started_at": parse_dt(elem.get("startDate")),
        "ended_at": parse_dt(elem.get("endDate")),
        "duration_s": duration_seconds(elem.get("duration"), elem.get("durationUnit")),
        "distance_m": distance,
        "active_kcal": energy,
        "avg_hr": as_float(hr.get("average")) if hr is not None else None,
        "min_hr": as_float(hr.get("minimum")) if hr is not None else None,
        "max_hr": as_float(hr.get("maximum")) if hr is not None else None,
        "stroke_count": as_float(strokes_stat.get("sum")) if strokes_stat is not None else None,
        "pool_length_m": pool_length_from_text(m2("HKLapLength", "HKMetadataKeyLapLength")),
        "external_id": m2("HKExternalUUID", "HKMetadataKeyExternalUUID"),
        "meta": meta,
    }


def _laps(elem, wid: str, pool_len: float | None) -> list[tuple]:
    """Per-length splits from HKWorkoutEventTypeLap events.

    A lap event carries a timestamp and a duration but no distance — in a pool
    one lap is one length by definition, so distance comes from the lap length.
    For anything without a known pool length the distance stays NULL rather than
    being guessed.
    """
    rows, idx = [], 0
    for ev in elem.findall("WorkoutEvent"):
        if ev.get("type") != "HKWorkoutEventTypeLap":
            continue
        ts = parse_dt(ev.get("date"))
        if not ts:
            continue
        idx += 1
        rows.append((
            wid, idx, ts,
            duration_seconds(ev.get("duration"), ev.get("durationUnit")),
            pool_len,
            None,
        ))
    return rows


# --------------------------------------------------------------------- scan --

async def scan(path: Path) -> None:
    """Report what the archive holds. Writes nothing."""
    types: Counter = Counter()
    unmapped: Counter = Counter()
    activities: Counter = Counter()
    sleep_values: Counter = Counter()
    lap_workouts = lap_events = 0
    stroke_style_seen = 0
    routes = 0
    first = last = None
    n_workouts = 0

    seen = 0
    for elem in _elements(path):
        seen += 1
        if seen % PROGRESS_EVERY == 0:
            print(f"  … {seen:,} elements", file=sys.stderr, flush=True)
        tag = elem.tag
        if tag == "Record":
            t = elem.get("type") or "?"
            types[t] += 1
            ts = parse_dt(elem.get("startDate"))
            if ts:
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
            if t == SLEEP_TYPE:
                sleep_values[elem.get("value") or "?"] += 1
            elif canonical(PROVIDER, t) is None:
                unmapped[t] += 1
        elif tag == "Workout":
            n_workouts += 1
            activities[elem.get("workoutActivityType") or "?"] += 1
            ts = parse_dt(elem.get("startDate"))
            if ts:
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
            laps = [e for e in elem.findall("WorkoutEvent")
                    if e.get("type") == "HKWorkoutEventTypeLap"]
            if laps:
                lap_workouts += 1
                lap_events += len(laps)
            for m in elem.findall("MetadataEntry"):
                if m.get("key") == "HKMetadataKeySwimmingStrokeStyle":
                    stroke_style_seen += 1
            routes += len(elem.findall("WorkoutRoute"))

    total = sum(types.values())
    print(f"\n=== {path.name} ===")
    print(f"records            {total:,}")
    print(f"workouts           {n_workouts:,}")
    print(f"covers             {first} .. {last}")

    print(f"\n--- workouts by activity ({len(activities)} kinds) ---")
    for a, n in activities.most_common():
        print(f"  {n:>7,}  {a}  -> {apple_sport(a)}")

    print("\n--- per-length splits (the CLAUDE.md assumption) ---")
    if lap_events:
        print(f"  LAP EVENTS PRESENT: {lap_events:,} across {lap_workouts:,} workouts.")
        print("  Real splits exist here. SWOLF can be measured, not estimated.")
    else:
        print("  No HKWorkoutEventTypeLap events found.")
        print("  CLAUDE.md holds: the top-quartile estimate stays the only option.")
    print(f"  swimming stroke-style metadata entries: {stroke_style_seen:,}")
    print(f"  embedded workout routes: {routes:,}")

    mapped = [(t, n) for t, n in types.most_common() if canonical(PROVIDER, t)]
    print(f"\n--- mapped record types ({len(mapped)} of {len(types)}) ---")
    for t, n in mapped:
        print(f"  {n:>9,}  {t}  -> {canonical(PROVIDER, t)}")

    if sleep_values:
        print("\n--- sleep stages ---")
        for v, n in sleep_values.most_common():
            print(f"  {n:>9,}  {v}  -> {sleep_stage(v)}")

    if unmapped:
        print(f"\n--- unmapped, will be skipped ({len(unmapped)} types) ---")
        for t, n in unmapped.most_common(40):
            print(f"  {n:>9,}  {t}")
        print("  These stay in the archive file; add them to app/canon.py to ingest.")


# ------------------------------------------------------------------- import --

async def run_import(path: Path, dsn: str, tz: str, commit_every: int = 200_000) -> dict:
    # Imported here, not at module scope, so `--scan` runs anywhere Python does.
    import psycopg

    sha = file_sha256(path)
    size = path.stat().st_size
    print(f"{path.name}: {size / 1e6:.1f} MB, sha256 {sha[:12]}", flush=True)

    counts = Counter()
    first = last = None

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(False)
        await upsert_metric_meta(conn, CANON)

        # Workouts already present from HAE pushes carry HealthKit UUIDs; the
        # same session in export.xml has no UUID at all. Matching on start time
        # is what keeps one swim from becoming two rows. Workout counts are in
        # the thousands, so the whole map fits in memory comfortably.
        cur = await conn.execute("SELECT id, started_at FROM workouts")
        existing = {int(ts.timestamp()): wid for wid, ts in await cur.fetchall()}
        print(f"{len(existing):,} workouts already in the database", flush=True)

        obs = BatchWriter(conn, "observations", OBS_COLS,
                          conflict=("provider", "metric", "ts", "source"))
        sleep = BatchWriter(conn, "sleep_segments", SLEEP_COLS,
                            conflict=("provider", "started_at", "stage", "source"))
        wk = BatchWriter(conn, "workouts", WORKOUT_COLS, conflict=("id",),
                         update=[c for c in WORKOUT_COLS if c != "id"],
                         # Where a swim was already loaded via HAE, keep the
                         # fields the archive does not carry rather than nulling
                         # them: the localised name/location, and pool length or
                         # indoor flag on any workout whose metadata lacks them.
                         # derive.py owns the SWOLF columns and they are not in
                         # WORKOUT_COLS here, so the archive never touches them.
                         coalesce=("name_raw", "location_raw", "is_indoor",
                                   "pool_length_m", "total_kcal"))
        laps = BatchWriter(conn, "workout_laps", LAP_COLS,
                           conflict=("workout_id", "idx"))

        since_commit = 0
        seen = 0
        for elem in _elements(path):
            seen += 1
            if seen % PROGRESS_EVERY == 0:
                print(f"  … read {seen:,} elements", flush=True)
            tag = elem.tag

            if tag == "Record":
                rtype = elem.get("type") or ""
                ts = parse_dt(elem.get("startDate"))
                if ts is None:
                    continue
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
                src = elem.get("sourceName") or ""

                if rtype == SLEEP_TYPE:
                    stage = sleep_stage(elem.get("value"))
                    end = parse_dt(elem.get("endDate"))
                    if stage and end:
                        await sleep.add((PROVIDER, ts, end, stage, src, night_day(ts, tz)))
                        counts["sleep"] += 1
                        since_commit += 1
                    continue

                metric = canonical(PROVIDER, rtype)
                if metric is None:
                    counts["skipped"] += 1
                    continue
                conv = to_canonical_value(metric, as_float(elem.get("value")),
                                          elem.get("unit"))
                if conv is None:
                    continue
                value, unit = conv
                await obs.add((PROVIDER, metric, ts, local_day(ts, tz), value, unit, src))
                counts["metrics"] += 1
                since_commit += 1

            elif tag == "Workout":
                f = _workout_fields(elem)
                start = f["started_at"]
                if start is None:
                    continue
                epoch = int(start.timestamp())
                wid = existing.get(epoch) or f"{PROVIDER}:{epoch}"
                existing[epoch] = wid

                # `raw` is the parsed shape, not the XML text: the archive file
                # itself is the byte-level record (see raw_files).
                raw = {"workoutActivityType": f["activity"], "metadata": f["meta"],
                       "sourceName": f["source"], "origin": "export.xml"}

                await wk.add((
                    wid, PROVIDER, f["external_id"], f["sport"], f["is_indoor"],
                    None, None, start, f["ended_at"],
                    local_day(start, tz), f["duration_s"], f["distance_m"],
                    f["active_kcal"], None, f["avg_hr"], f["min_hr"], f["max_hr"],
                    f["pool_length_m"], f["stroke_count"], json.dumps(raw),
                ))
                counts["workouts"] += 1
                since_commit += 1

                lap_rows = _laps(elem, wid, f["pool_length_m"])
                if lap_rows:
                    # Laps reference the workout by foreign key, so the workout
                    # has to have landed before they can be written.
                    await wk.flush()
                    await laps.extend(lap_rows)
                    counts["laps"] += len(lap_rows)

            if since_commit >= commit_every:
                await obs.flush()
                await sleep.flush()
                await wk.flush()
                await laps.flush()
                await conn.commit()
                since_commit = 0
                print(f"  … {counts['metrics']:,} metrics, {counts['workouts']:,} workouts,"
                      f" {counts['sleep']:,} sleep segments", flush=True)

        await obs.flush()
        await sleep.flush()
        await wk.flush()
        await laps.flush()

        await register_raw_file(
            conn, str(path), sha, "apple_xml", size,
            counts={"metrics": counts["metrics"], "workouts": counts["workouts"],
                    "samples": counts["sleep"]},
            covers=(first, last),
            note=f"laps={counts['laps']} skipped_records={counts['skipped']}",
        )
        await conn.commit()

    print(f"\ndone: {dict(counts)}")
    print(f"covers {first} .. {last}")
    if counts["laps"]:
        print(f"{counts['laps']:,} lap splits imported — real per-length data is available")
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("path", type=Path)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true",
                      help="report what the file contains, write nothing (default)")
    mode.add_argument("--import", dest="do_import", action="store_true",
                      help="parse and load into Postgres")
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--tz", default=TZ)
    args = ap.parse_args(argv)

    if not args.path.exists():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 1

    if args.do_import:
        asyncio.run(run_import(args.path, args.dsn, args.tz))
    else:
        asyncio.run(scan(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
