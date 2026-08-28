# Backfilling the full history

The database started with roughly ten months of data — an artefact of which file
was exported from the phone, not a limit in the code. This is how to load
everything.

## Why two import paths exist

|  | Native `export.xml` | Health Auto Export |
|---|---|---|
| Coverage | everything HealthKit holds, back to the first iPhone | whatever window you pick |
| Automatable | **no** — Apple exposes no API and no Shortcuts action; it is a manual share-sheet export | yes, scheduled REST push |
| Detail | `<WorkoutEvent>` laps, per-record provenance | per-minute series, pre-summarised |
| Practical size | hundreds of MB, one file | 100 MB caps out at ~10 months |

So the archive covers the past and HAE covers the present. Neither replaces the
other, and HAE cannot be dropped.

## Three limits that make "just post a bigger file" fail

1. **Cloudflare free plan caps request bodies at 100 MB.** Large imports go over
   the LAN, or not over HTTP at all.
2. **`json.loads` of a multi-hundred-MB body needs several GB of RAM** — more
   than a Pi 5 has spare. Both importers stream instead.
3. **A Postgres `jsonb` value tops out around 255 MB**, so a full archive cannot
   be inlined into `raw_payloads`. Big files stay on `/share` and are registered
   in `raw_files` by digest; a replay reads them back off the disk.

## Steps

### 1. Export from the phone

Health → profile picture → **Export All Health Data**. It takes several minutes
and produces `export.zip`. Put it in the HA `share` folder over Samba:

```
/share/archive/export.zip
```

Unzip it there; the useful file is `apple_health_export/export.xml`, and newer
iOS versions also include `workout-routes/*.gpx`.

### 2. Look before loading

```bash
docker exec addon_local_longcourse \
  python -m importers.apple_xml --scan /share/archive/apple_health_export/export.xml
```

`--scan` writes nothing. It reports how far back the archive goes, which record
types it contains, which of them are mapped in `app/canon.py`, and — the
interesting one — whether swim workouts carry `HKWorkoutEventTypeLap` events.

**If lap events are present**, real per-length splits exist and SWOLF becomes a
measurement rather than the top-quartile estimate described in CLAUDE.md. That
would revise a documented conclusion, so record the result there either way.

Anything in the "unmapped" list is skipped, not lost — it stays in the archive.
Add it to `app/canon.py` and re-run if it turns out to be wanted.

### 3. Import

```bash
docker exec addon_local_longcourse \
  python -m importers.apple_xml --import /share/archive/apple_health_export/export.xml
```

Commits every 200k rows and prints progress. Safe to re-run: every write is an
upsert, so an interrupted import resumes rather than duplicating.

Workouts already loaded from HAE carry HealthKit UUIDs; the same session in
`export.xml` has no UUID at all, so the importer matches on start time and
reuses the existing row's id. Without that one swim would become two.

### 4. Re-derive

```bash
docker exec addon_local_longcourse python -m derive --all
```

This builds per-minute workout series from the standalone records the archive
uses, then recomputes lengths, active time, SWOLF and pace — preferring real lap
splits where they exist. It reports how many sessions used each method.

Note the bucketing: archive records are far finer than HAE's per-minute series,
and the pace estimator's answer depends on the resolution of its input. They are
bucketed to one minute so a backfilled session and a pushed one are comparable.

### 5. Check

```sql
-- history depth and shape
SELECT date_trunc('year', started_at)::date AS yr, sport, provider,
       COUNT(*), ROUND(SUM(distance_m)) AS m
FROM workouts GROUP BY 1,2,3 ORDER BY 1,2;

-- the golden numbers from CLAUDE.md must still hold for 50m sessions
SELECT sport, is_indoor, pool_length_m, swolf_method, count(*),
       round(avg(swolf)::numeric,1) swolf,
       round(avg(moving_s/nullif(duration_s,0))::numeric,2) ratio,
       round(avg(pace_s_per_100m)::numeric) pace
FROM workouts GROUP BY 1,2,3,4 ORDER BY 5 DESC;

-- no session should appear twice
SELECT started_at, COUNT(*) FROM workouts
GROUP BY 1 HAVING COUNT(*) > 1 ORDER BY 1 DESC;

-- steps must not have doubled: compare against the Health app
SELECT time, value, source FROM v_daily_metrics
WHERE metric = 'steps' ORDER BY time DESC LIMIT 14;
```

Then post one fresh HAE push and confirm it adds to the history rather than
replacing it, and that `/health` reports a recent `age_hours`.

### 6. Import GPX routes

The archive links each route to its workout directly — a `<Workout>` carries
`<WorkoutRoute><FileReference path="/workout-routes/route_*.gpx"/>` — so no
filename matching is needed.

```bash
docker exec addon_local_longcourse \
  python -m importers.routes /share/archive/apple_health_export/export.xml
```

It reads export.xml for each workout's route file, resolves the workout id by
start time, parses the GPX and fills `route_points` (one row per GPS fix, full
resolution) plus a `workout_routes` summary. Idempotent. ~760k points for a
four-year archive; open-water swims are the subset the Open Water dashboard maps.

## Samsung Health

Not built yet — waiting on the archive. It covers an earlier, non-overlapping
period, so it lands as `provider='samsung'` with no cross-provider deduplication
needed. The columns and the canonical vocabulary are already in place; what is
missing is `app/importers/samsung.py` and the alias table in `app/canon.py`.

After importing, confirm the periods really do not overlap:

```sql
SELECT provider, MIN(started_at), MAX(started_at), COUNT(*)
FROM workouts GROUP BY 1;
```

If they do overlap, dedup rules are needed before any total is trustworthy.
