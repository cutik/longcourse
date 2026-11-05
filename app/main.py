"""
Longcourse — Apple Health ingest + MCP, one process.

Routes:
  POST /ingest   Health Auto Export REST automation target (bearer auth)
  GET  /health   freshness probe for the Home Assistant watchdog sensor
  ANY  /mcp      MCP over HTTP for Claude Code / claude.ai

Background: when `garmin_enabled`, a loop pulls Garmin Connect every
`garmin_poll_minutes` (importers/garmin.py). Same process, no cron.

Invariants:
  1. The raw body is archived before anything is parsed. If the parser is wrong
     (and it has been), we replay instead of losing data.
  2. Every write is an upsert. HAE resends overlapping windows on every run.
  3. Nothing is classified by a user-visible string. Workout names and
     locations come back in the phone's language.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from canon import CANON, canonical, local_day, night_day, sleep_stage, to_canonical_value
from db import BatchWriter, upsert_metric_meta
from parse import kcal, parse_dt, pool_length_m, qty, to_meters
from importers.routes import hae_points, route_writers, write_route
import localtime
from derive import inherit_sport_by_name, mark_superseded
from swim import SERIES_KEYS, classify, derive_swim, hae_laps
from sleep import sleep_rows as _sleep_rows
from settings import (DSN, GARMIN_ENABLED, GARMIN_LOOKBACK_DAYS, GARMIN_POLL_MINUTES,
                      HAE_ENABLED, TOKEN, TZ)

SCHEMA = Path(__file__).with_name("schema.sql")

pool: AsyncConnectionPool | None = None

PROVIDER = "hae"        # dialect key for canon.py; the data itself is Apple's
PROVIDER_ROW = "apple"  # what lands in the provider column, shared with export.xml


# ----------------------------------------------------------------- parsing --
# Value parsing lives in parse.py so the CLI importers can share it.

# Column order for the workout upsert. Named explicitly because the tuple built
# by workout_row() has to line up with it, and a silent misalignment writes
# plausible-looking numbers into the wrong columns.
WORKOUT_COLS = (
    "id", "provider", "external_id", "sport", "is_indoor", "name_raw", "location_raw",
    "started_at", "ended_at", "local_day", "duration_s", "distance_m",
    "active_kcal", "total_kcal", "avg_hr", "min_hr", "max_hr", "hr_recovery",
    "pool_length_m", "stroke_count", "swim_cadence",
    "lengths", "moving_s", "swolf", "swolf_gross", "pace_s_per_100m", "swolf_method",
    "raw",
)


def workout_row(w: dict, wid_for_start: dict[int, str] | None = None) -> tuple | None:
    """Map one HAE workout onto WORKOUT_COLS.

    `wid_for_start` maps a start-time epoch to an id already in the database.
    The same session imported from export.xml has no HealthKit UUID and got a
    synthetic id; reusing it here is what stops one swim becoming two rows.
    """
    wid = w.get("id") or w.get("uuid")
    start = parse_dt(w.get("start"))
    if not wid or not start:
        return None
    external_id = str(wid)
    if wid_for_start:
        wid = wid_for_start.get(int(start.timestamp()), wid)

    sport = classify(w)
    duration_s = qty(w.get("duration"))
    distance_m = to_meters(w.get("distance"))
    lap = pool_length_m(w.get("lapLength"))
    strokes = qty(w.get("totalSwimmingStrokeCount"))

    hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
    hrr_series = w.get("heartRateRecovery") or []
    hrr = None
    if isinstance(hrr_series, list) and hrr_series:
        first = hrr_series[0]
        hrr = first.get("Avg") if isinstance(first, dict) else None

    # Derived swim figures are computed at ingest so every consumer sees the
    # same numbers, and recomputed in bulk by derive.py when the formula
    # changes — that path no longer needs the original payload re-posted.
    # Real per-length laps, when HAE sends them, win over the estimate.
    laps = hae_laps(w) if sport == "swim" else []
    d = (derive_swim(distance_m, duration_s, lap, strokes, w.get("swimDistance") or [],
                     lap_count=len(laps) or None,
                     lap_total_s=sum(l[2] for l in laps) or None)
         if sport == "swim" else
         {"lengths": None, "moving_s": None, "swolf": None, "swolf_gross": None,
          "pace_s_per_100m": None, "swolf_method": None})
    lap = lap or d.get("pool_length_m")

    return (
        str(wid), PROVIDER_ROW, external_id,
        sport, w.get("isIndoor"), w.get("name"), w.get("location"),
        start, parse_dt(w.get("end")), local_day(start, TZ), duration_s, distance_m,
        kcal(w.get("activeEnergyBurned")), kcal(w.get("totalEnergy")),
        qty(w.get("avgHeartRate")) or qty(hr.get("avg")),
        qty(hr.get("min")), qty(w.get("maxHeartRate")) or qty(hr.get("max")),
        hrr, lap, strokes, qty(w.get("swimCadence")),
        d["lengths"], d["moving_s"], d["swolf"], d["swolf_gross"],
        d["pace_s_per_100m"], d["swolf_method"],
        json.dumps(w),
    )


def sample_rows(wid: str, w: dict) -> list[tuple]:
    rows = []
    for key in SERIES_KEYS:
        series = w.get(key)
        if not isinstance(series, list):
            continue
        for p in series:
            if not isinstance(p, dict):
                continue
            ts = parse_dt(p.get("date"))
            if not ts:
                continue
            # HR buckets carry Min/Avg/Max instead of qty, with capitalised keys.
            value = qty(p)
            if value is None and "Avg" in p:
                value = p.get("Avg")
            extra = {k: v for k, v in p.items()
                     if k not in {"date", "qty", "units", "source"}} or None
            rows.append((wid, key, ts, value, p.get("units"),
                         json.dumps(extra) if extra else None))
    return rows


METRIC_COLS = ("name", "ts", "source", "qty", "units", "extra", "provider")
OBS_COLS = ("provider", "metric", "ts", "local_day", "value", "unit", "source")
SLEEP_COLS = ("provider", "started_at", "ended_at", "stage", "source", "local_day")
SAMPLE_COLS = ("workout_id", "metric", "ts", "qty", "units", "extra")
LAP_COLS = ("workout_id", "idx", "started_at", "duration_s", "distance_m", "stroke_style")


async def store(payload: dict, sha: str) -> dict:
    """Ingest one Health Auto Export push.

    Writes go to two layers. `metrics`/`workouts`/`workout_samples` keep HAE's
    own names and units — the raw landing zone, unchanged since v1 so a replay
    of any archived payload still produces identical rows. `observations` and
    `sleep_segments` hold the canonical, unit-normalised, provider-tagged view
    that the dashboards read.

    Everything is batched. The previous version issued one round-trip per data
    point, which was survivable for an hourly push and is not survivable for a
    backfill.
    """
    data = payload.get("data", payload)
    counts: dict[str, int] = {"metrics": 0, "workouts": 0, "samples": 0,
                              "observations": 0, "sleep": 0, "route_points": 0}

    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO raw_payloads (body_sha256, body) VALUES (%s,%s) "
            "ON CONFLICT (body_sha256) DO NOTHING",
            (sha, json.dumps(payload)),
        )
        # Commit the archive on its own, so a parser failure below rolls back
        # only the parsed rows. Before this, an exception took the raw body
        # down with it and there was nothing left to replay.
        await conn.commit()

        raw_m = BatchWriter(conn, "metrics", METRIC_COLS,
                            conflict=("name", "ts", "source"))
        obs = BatchWriter(conn, "observations", OBS_COLS,
                          conflict=("provider", "metric", "ts", "source"))
        sleep = BatchWriter(conn, "sleep_segments", SLEEP_COLS,
                            conflict=("provider", "started_at", "stage", "source"))
        wk = BatchWriter(conn, "workouts", WORKOUT_COLS, conflict=("id",))
        samples = BatchWriter(conn, "workout_samples", SAMPLE_COLS,
                              conflict=("workout_id", "metric", "ts"))
        rpts, rsum = route_writers(conn)
        laps = BatchWriter(conn, "workout_laps", LAP_COLS, conflict=("workout_id", "idx"))
        pushed: list[tuple[str, str | None]] = []

        for m in data.get("metrics") or []:
            name, u = m.get("name"), m.get("units")
            if not name:
                continue
            metric = canonical(PROVIDER, name)
            for p in m.get("data") or []:
                ts = parse_dt(p.get("date"))
                if not ts:
                    continue
                src = p.get("source") or ""
                extra = {k: v for k, v in p.items()
                         if k not in {"date", "qty", "source", "units"}} or None
                await raw_m.add((name, ts, src, qty(p), u,
                                 json.dumps(extra) if extra else None, PROVIDER_ROW))
                counts["metrics"] += 1

                if name == "sleep_analysis":
                    for row in _sleep_rows(p):
                        await sleep.add(row)
                        counts["sleep"] += 1

                if metric:
                    conv = to_canonical_value(metric, qty(p), u)
                    if conv:
                        value, unit = conv
                        await obs.add((PROVIDER_ROW, metric, ts, local_day(ts, TZ),
                                       value, unit, src))
                        counts["observations"] += 1

        workouts = data.get("workouts") or []
        if workouts:
            # A session already imported from export.xml has a synthetic id and
            # no HealthKit UUID to match on; start time is the only shared key.
            starts = [int(t.timestamp()) for t in
                      (parse_dt(w.get("start")) for w in workouts) if t]
            # Apple rows only. Garmin's copy of a session has the same start to
            # the second (Garmin Connect writes it into Apple Health), and
            # matching it here would overwrite the Garmin row with HAE's
            # poorer data. Cross-provider duplicates are mark_superseded's job.
            cur = await conn.execute(
                "SELECT id, started_at FROM workouts WHERE provider = 'apple'"
                " AND started_at = ANY("
                " SELECT to_timestamp(x) FROM unnest(%s::bigint[]) AS x)", (starts,))
            by_start = {int(ts.timestamp()): wid for wid, ts in await cur.fetchall()}
        else:
            by_start = {}

        for w in workouts:
            row = workout_row(w, by_start)
            if not row:
                continue
            await wk.add(row)
            counts["workouts"] += 1
            # workout_samples has a foreign key onto workouts, so the parent
            # rows have to be committed to the table before the children land.
            await wk.flush()
            for sr in sample_rows(row[0], w):
                await samples.add(sr)
                counts["samples"] += 1
            for idx, start, dur, dist in (hae_laps(w) if row[3] == "swim" else []):
                await laps.add((row[0], idx, start, dur, dist, None))
            pushed.append((row[0], w.get("start")))

            # The rolling window resends the same track every hour; rewrite it
            # only when the fix count changed.
            route = hae_points(w.get("route"))
            if route:
                cur = await conn.execute(
                    "SELECT point_count FROM workout_routes WHERE workout_id = %s",
                    (row[0],))
                have = await cur.fetchone()
                if not have or have[0] != len(route):
                    counts["route_points"] += await write_route(
                        conn, rpts, rsum, row[0], "hae", route)

        for writer in (raw_m, obs, sleep, wk, samples, rpts, rsum, laps):
            await writer.flush()

        if pushed:
            await inherit_sport_by_name(conn)
            await localtime.resolve(conn, pushed)
            # Garmin Connect writes its activities into Apple Health, so a push
            # can carry a session the Garmin pull already has.
            await mark_superseded(conn)

        await conn.execute(
            "INSERT INTO ingest_log (n_metrics,n_workouts,n_samples) VALUES (%s,%s,%s)",
            (counts["metrics"], counts["workouts"], counts["samples"]),
        )

    return counts


# --------------------------------------------------------------------- MCP --
# Deliberately not a SQL passthrough. Each tool answers a coaching question and
# returns a small, pre-aggregated payload.

mcp = FastMCP("longcourse")


async def q(sql: str, params: tuple = ()) -> list[dict]:
    # The row factory goes on the cursor, not the connection. Setting it on a
    # pooled connection outlives this call: the next /ingest that drew the same
    # connection got dicts back, unpacked their keys as (id, started_at) and
    # died on 'str' object has no attribute 'timestamp' — dropping every push
    # that carried an already-known workout, once the watchdog polled /health.
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(sql, params)
        return await cur.fetchall()


def _r(rows: list[dict], nd: int = 1) -> list[dict]:
    return [{k: (round(v, nd) if isinstance(v, float) else v) for k, v in row.items()}
            for row in rows]


async def freshness() -> dict:
    """Last ingest per provider, plus which provider is primary.

    Primary is Garmin when its pull is enabled, else Apple — the watchdog and
    sync_status judge staleness on the primary, since a quiet secondary (HAE
    still pushing iPhone steps, say) is not a broken pipeline.
    """
    rows = await q("SELECT DISTINCT ON (provider) provider, at, n_metrics, n_workouts, note"
                   " FROM ingest_log ORDER BY provider, at DESC")
    now = datetime.now().astimezone()
    providers = {r["provider"]: {
        "last_ingest": r["at"].isoformat(),
        "age_hours": round((now - r["at"]).total_seconds() / 3600, 1),
        "last_metrics": r["n_metrics"], "last_workouts": r["n_workouts"],
        **({"note": r["note"]} if r["note"] else {}),
    } for r in rows}
    primary = "garmin" if GARMIN_ENABLED else "apple"
    return {"primary": primary, "providers": providers,
            "enabled": {"apple": HAE_ENABLED, "garmin": GARMIN_ENABLED}}


@mcp.tool()
async def sync_status() -> dict:
    """Check whether the data pipeline is alive and how fresh each source is.

    Two providers: 'garmin' (pulled from Garmin Connect hourly by the server)
    and 'apple' (pushed by Health Auto Export from the iPhone — since the move
    to Garmin this mostly carries iPhone steps and gait). Status is judged on
    the primary. Call this first if other tools return suspiciously empty
    results.
    """
    f = await freshness()
    p = f["providers"].get(f["primary"])
    if not p:
        return {**f, "status": "never_synced"}
    return {**f, "status": "ok" if p["age_hours"] < 12 else "stale"}


@mcp.tool()
async def swim_sessions(since_days: int = 28, pool_length_m: float | None = None,
                        indoor_only: bool = True, provider: str | None = None) -> list[dict]:
    """List swim sessions with pace and efficiency, newest first.

    SWOLF here excludes rest between sets, so interval and steady sessions are
    directly comparable; swolf_gross includes rest and is only useful for
    spotting how much of a session was spent standing at the wall.

    Filter by pool_length_m before comparing SWOLF across sessions: it sums
    strokes and seconds per length, so a 50m course yields structurally
    different numbers than 25m even when technique is unchanged.

    Likewise never compare SWOLF or strokes across `provider`: Apple Watch
    (history to 2026-10-24) and Garmin (since) count strokes differently.
    Pace is comparable — both measure active time from per-length splits.
    """
    return _r(await q(
        """
        SELECT id, provider, started_at::date AS day, distance_m, duration_s, moving_s,
               pool_length_m, lengths, stroke_count, swim_cadence,
               swolf, swolf_gross, swolf_method, pace_s_per_100m, avg_hr, max_hr, hr_recovery
        FROM workouts
        WHERE sport = 'swim' AND superseded_by IS NULL
          AND started_at >= now() - make_interval(days => %s)
          AND (%s::boolean IS NOT TRUE OR is_indoor IS TRUE)
          AND (%s::float8 IS NULL OR pool_length_m = %s)
          AND (%s::text IS NULL OR provider = %s)
        ORDER BY started_at DESC
        """, (since_days, indoor_only, pool_length_m, pool_length_m, provider, provider)))


@mcp.tool()
async def session_detail(workout_id: str, bucket_minutes: int = 5) -> dict:
    """One session's summary plus its within-session shape, bucketed over time.

    Per-minute stroke count, distance and heart rate, aggregated into buckets.
    Falling distance with steady stroke rate suggests fatigue; gaps with
    near-zero distance are rests and mark set boundaries. Do not present these
    buckets as 50m splits — real per-length splits, where they exist, are in
    `lengths` (Garmin labels each with its stroke style; Apple never did).
    """
    rows = await q("SELECT * FROM workouts WHERE id = %s", (workout_id,))
    if not rows:
        return {"error": "not found"}
    w = rows[0]
    w.pop("raw", None)
    lengths = await q(
        "SELECT idx, duration_s, distance_m, stroke_style FROM workout_laps"
        " WHERE workout_id = %s ORDER BY idx", (workout_id,))
    buckets = await q(
        """
        SELECT to_char(date_trunc('hour', ts)
                 + make_interval(mins => (EXTRACT(minute FROM ts)::int
                     / %s) * %s), 'HH24:MI') AS t,
               AVG(qty) FILTER (WHERE metric = 'swimStroke')   AS stroke_rate,
               SUM(qty) FILTER (WHERE metric = 'swimDistance') AS distance_m,
               AVG(qty) FILTER (WHERE metric = 'heartRateData') AS hr
        FROM workout_samples
        WHERE workout_id = %s AND metric IN ('swimStroke','swimDistance','heartRateData')
        GROUP BY 1 ORDER BY 1
        """, (bucket_minutes, bucket_minutes, workout_id))
    return {"session": _r([w])[0], "buckets": _r(buckets), "lengths": _r(lengths)}


@mcp.tool()
async def recovery_window(around: str, days: int = 3) -> list[dict]:
    """Daily HRV, resting heart rate, sleep and respiratory rate around a date (YYYY-MM-DD).

    Use this to judge whether a slow session reflects fatigue rather than technique.
    """
    d = date.fromisoformat(around)
    return _r(await q(
        """
        SELECT ts::date AS day, name, AVG(qty) AS avg, MIN(qty) AS min, MAX(qty) AS max
        FROM metrics
        WHERE name IN ('heart_rate_variability','resting_heart_rate','sleep_analysis',
                       'respiratory_rate','vo2_max')
          AND ts::date BETWEEN %s AND %s
        GROUP BY 1,2 ORDER BY 1,2
        """, (d - timedelta(days=days), d + timedelta(days=days))))


@mcp.tool()
async def compare_blocks(block_days: int = 14) -> dict:
    """Compare the last N days of swimming with the N days before that.

    Returns volume, pace and average SWOLF per block so trends can be stated as
    a delta instead of eyeballed from a session list.
    """
    rows = await q(
        """
        WITH s AS (
          SELECT distance_m, moving_s, swolf,
                 CASE WHEN started_at >= now() - make_interval(days => %s)
                      THEN 'current' ELSE 'previous' END AS block
          FROM workouts
          WHERE sport = 'swim' AND superseded_by IS NULL
            AND started_at >= now() - make_interval(days => %s))
        SELECT block, COUNT(*) AS sessions, SUM(distance_m) AS distance_m,
               SUM(moving_s) AS moving_s, AVG(swolf) AS avg_swolf,
               SUM(moving_s)/NULLIF(SUM(distance_m)/100.0,0) AS pace_s_per_100m
        FROM s GROUP BY block
        """, (block_days, block_days * 2))
    return {r["block"]: _r([r])[0] for r in rows}


@mcp.tool()
async def css_inputs() -> dict:
    """Fastest recent 400m and 200m efforts — the inputs for a Critical Swim Speed test.

    CSS = (400 - 200) / (t400 - t200). Without a recent test the training zones
    are guesswork, so if the best efforts here are older than about six weeks,
    recommend re-testing before writing a plan.

    These are whole sessions, not timed sets. A 400m session that was really a
    warmup will look like a bad time trial — confirm before using the numbers.
    """
    rows = await q(
        """
        SELECT started_at::date AS day, distance_m, moving_s, pool_length_m
        FROM workouts
        WHERE sport = 'swim' AND superseded_by IS NULL AND distance_m BETWEEN 180 AND 420
        ORDER BY moving_s/NULLIF(distance_m,0) ASC LIMIT 10
        """)
    return {"candidates": _r(rows),
            "note": "Whole sessions. Verify these were maximal time trials."}


# --------------------------------------------------------------------- app --

mcp_app = mcp.http_app(path="/mcp")


async def apply_schema() -> None:
    """Run schema.sql on every boot.

    Every statement is CREATE ... IF NOT EXISTS, so this is idempotent. A
    missing file is fatal on purpose: skipping it quietly produces a server
    that starts cleanly and then 500s on the first query.
    """
    if not SCHEMA.exists():
        raise RuntimeError(f"{SCHEMA} missing — the image did not COPY db/schema.sql")
    async with pool.connection() as conn:
        await conn.execute(SCHEMA.read_text())
        # metric_meta mirrors app/canon.py so the Grafana views can join against
        # it. Rewritten every boot, so the code stays the single definition.
        await upsert_metric_meta(conn, CANON)
    print(f"schema applied from {SCHEMA}", flush=True)


async def garmin_loop() -> None:
    """Pull the last few days from Garmin Connect, forever.

    A failure (expired tokens, a 429, Garmin changing its login again) is
    logged into ingest_log and retried next round; it never takes the server
    down — /ingest and the dashboards keep working without Garmin. The client
    is kept between rounds so tokens are refreshed rather than re-read.
    """
    from importers.garmin import client, pull
    g = None
    while True:
        try:
            if g is None:
                g = await asyncio.to_thread(client)
            async with pool.connection() as conn:
                until = date.today()
                counts = await pull(conn, until - timedelta(days=GARMIN_LOOKBACK_DAYS),
                                    until, g)
            print(f"garmin pull: {counts}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            g = None                                            # re-login next round
            print(f"garmin pull failed: {type(e).__name__}: {e}", flush=True)
            try:
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO ingest_log (provider, note) VALUES ('garmin_error', %s)",
                        (f"{type(e).__name__}: {e}"[:500],))
            except Exception:                                   # noqa: BLE001
                pass
        await asyncio.sleep(GARMIN_POLL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    # check= pings a connection before handing it out. Without it a Postgres
    # restart (the add-on updates on its own) left dead connections in the
    # pool, and the next /health 500'd with AdminShutdown.
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False,
                               check=AsyncConnectionPool.check_connection)
    await pool.open()
    await apply_schema()
    task = asyncio.create_task(garmin_loop()) if GARMIN_ENABLED else None
    async with mcp_app.lifespan(app):
        yield
    if task:
        task.cancel()
    await pool.close()


app = FastAPI(title="longcourse", lifespan=lifespan)


def auth(authorization: str = Header(default="")) -> None:
    """Bearer check for /ingest.

    Fails closed when no token is configured: with `ingest_token` empty the old
    comparison built the string "Bearer None" and happily accepted anyone who
    sent it. Constant-time compare because this endpoint is reachable from the
    internet through the Cloudflare tunnel.
    """
    if not TOKEN:
        raise HTTPException(status_code=503, detail="ingest_token not configured")
    if not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(status_code=401, detail="bad token")


@app.post("/ingest", dependencies=[Depends(auth)])
async def ingest(request: Request) -> dict:
    if not HAE_ENABLED:
        # 404 rather than 503: HAE should stop retrying, not back off.
        raise HTTPException(status_code=404, detail="hae ingest disabled")
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="not json")
    counts = await store(payload, hashlib.sha256(body).hexdigest())
    return {"ok": True, **counts}


@app.get("/health")
async def health() -> dict:
    """Target for the Home Assistant REST sensor.

    The top-level fields describe the PRIMARY provider (Garmin when enabled),
    so the existing watchdog keeps working unchanged; `providers` has each one,
    and `providers.garmin_error` the last failed pull, if any.
    """
    f = await freshness()
    p = f["providers"].get(f["primary"])
    if not p:
        return {"ok": True, "last_ingest": None, "age_hours": 999, **f}
    return {"ok": True, **p, **f}


app.mount("/", mcp_app)
