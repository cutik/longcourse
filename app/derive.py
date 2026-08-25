"""
Rebuild everything that is computed rather than measured.

    docker exec addon_local_longcourse python -m derive --all
    docker exec addon_local_longcourse python -m derive --since 2026-01-01

Why this is a separate job: derived columns used to be written only at ingest,
so changing the SWOLF formula meant `TRUNCATE workouts CASCADE` followed by
re-POSTing a 122 MB export — and on the full history that stops being viable.
Now the formula can change and be re-applied from data already in Postgres.

Two steps, in order:

1. workout_samples are filled from `observations` for any workout that has none.
   Health Auto Export ships per-minute series inside each workout; export.xml
   does not — it emits standalone <Record> elements which only relate to a
   workout by falling inside its time window. This joins them back up, bucketed
   to one minute so a backfilled session and a pushed one feed the estimator
   the same shape of input. Without that the same swim scores differently
   depending on which importer happened to load it.

2. Derived swim columns are recomputed, preferring real lap splits over the
   top-quartile estimate wherever laps exist.

Idempotent: re-running changes nothing unless the inputs or the formula did.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

import psycopg

from settings import DSN, TZ
from swim import derive_swim

# Canonical observation -> the workout_samples metric name the rest of the code
# already speaks. Keeping HAE's names here means session_detail, the estimator
# and the dashboards need no changes to read backfilled sessions.
OBS_TO_SAMPLE = {
    "distance_swimming": ("swimDistance", "SUM"),
    "swimming_strokes": ("swimStroke", "SUM"),
    "heart_rate": ("heartRateData", "AVG"),
    "active_energy": ("activeEnergy", "SUM"),
}


async def fill_samples_from_observations(conn, since: datetime | None) -> int:
    """Reconstruct per-minute workout series from standalone observations.

    Only workouts that have no samples for that metric are touched, so a session
    that arrived through HAE with its own series is never overwritten by a
    coarser reconstruction.
    """
    total = 0
    for obs_metric, (sample_metric, agg) in OBS_TO_SAMPLE.items():
        cur = await conn.execute(
            f"""
            INSERT INTO workout_samples (workout_id, metric, ts, qty, units, extra)
            SELECT w.id,
                   %s,
                   date_trunc('minute', o.ts),
                   {agg}(o.value),
                   MIN(o.unit),
                   NULL
            FROM workouts w
            JOIN observations o
              ON o.provider = w.provider
             AND o.metric   = %s
             AND o.ts      >= w.started_at
             AND o.ts      <= COALESCE(w.ended_at, w.started_at + interval '6 hours')
            WHERE (%s::timestamptz IS NULL OR w.started_at >= %s)
              AND NOT EXISTS (
                    SELECT 1 FROM workout_samples s
                    WHERE s.workout_id = w.id AND s.metric = %s)
            GROUP BY w.id, date_trunc('minute', o.ts)
            ON CONFLICT (workout_id, metric, ts) DO NOTHING
            """,
            (sample_metric, obs_metric, since, since, sample_metric),
        )
        n = cur.rowcount or 0
        total += n
        if n:
            print(f"  {sample_metric:<16} {n:>8,} buckets built from {obs_metric}")
    return total


async def rederive_workouts(conn, since: datetime | None) -> dict:
    """Recompute lengths / moving_s / swolf / pace for every swim."""
    cur = await conn.execute(
        """
        SELECT w.id, w.distance_m, w.duration_s, w.pool_length_m, w.stroke_count,
               l.lap_count, l.lap_total_s
        FROM workouts w
        LEFT JOIN (
            SELECT workout_id, COUNT(*) AS lap_count, SUM(duration_s) AS lap_total_s
            FROM workout_laps GROUP BY workout_id
        ) l ON l.workout_id = w.id
        WHERE w.sport = 'swim'
          AND (%s::timestamptz IS NULL OR w.started_at >= %s)
        ORDER BY w.started_at
        """, (since, since))
    rows = await cur.fetchall()
    print(f"  {len(rows):,} swim sessions to re-derive")

    stats = {"lap": 0, "estimated": 0, "none": 0}
    updates = []
    for wid, dist, dur, pool, strokes, lap_count, lap_total in rows:
        scur = await conn.execute(
            "SELECT ts, qty FROM workout_samples "
            "WHERE workout_id = %s AND metric = 'swimDistance' ORDER BY ts", (wid,))
        series = [{"date": ts.isoformat(), "qty": q} for ts, q in await scur.fetchall()]

        d = derive_swim(dist, dur, pool, strokes, series,
                        lap_count=lap_count, lap_total_s=lap_total)
        stats[d["swolf_method"] or "none"] += 1
        updates.append((d["lengths"], d["moving_s"], d["swolf"], d["swolf_gross"],
                        d["pace_s_per_100m"], d["swolf_method"], wid))

    async with conn.cursor() as c:
        await c.executemany(
            "UPDATE workouts SET lengths=%s, moving_s=%s, swolf=%s, swolf_gross=%s,"
            " pace_s_per_100m=%s, swolf_method=%s, updated_at=now() WHERE id=%s",
            updates)
    return stats


async def backfill_local_days(conn) -> int:
    """Populate local_day on rows written before the column existed."""
    cur = await conn.execute(
        "UPDATE workouts SET local_day = (started_at AT TIME ZONE %s)::date "
        "WHERE local_day IS NULL", (TZ,))
    return cur.rowcount or 0


async def run(dsn: str, since: datetime | None) -> None:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        print("backfilling local_day …")
        n = await backfill_local_days(conn)
        print(f"  {n:,} workouts dated")

        print("building workout series from observations …")
        await fill_samples_from_observations(conn, since)

        print("re-deriving swim figures …")
        stats = await rederive_workouts(conn, since)

        await conn.commit()

    print(f"\ndone: {stats['lap']:,} from real lap splits, "
          f"{stats['estimated']:,} estimated, {stats['none']:,} with neither")
    if stats["lap"]:
        print("Sessions differ in method — filter or group on swolf_method before\n"
              "comparing SWOLF, and never average the two together.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Recompute derived columns in place.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="every session on record")
    g.add_argument("--since", type=datetime.fromisoformat, metavar="YYYY-MM-DD")
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args(argv)
    asyncio.run(run(args.dsn, None if args.all else args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
