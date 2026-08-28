"""
Longcourse — Apple Health ingest + MCP, one process.

Routes:
  POST /ingest   Health Auto Export REST automation target (bearer auth)
  GET  /health   freshness probe for the Home Assistant watchdog sensor
  ANY  /mcp      MCP over HTTP for Claude Code / claude.ai

Invariants:
  1. The raw body is archived before anything is parsed. If the parser is wrong
     (and it has been), we replay instead of losing data.
  2. Every write is an upsert. HAE resends overlapping windows on every run.
  3. Nothing is classified by a user-visible string. Workout names and
     locations come back in the phone's language.
"""

from __future__ import annotations

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
from swim import SERIES_KEYS, classify, derive_swim
from sleep import sleep_rows as _sleep_rows
from settings import DSN, TOKEN, TZ

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
    d = (derive_swim(distance_m, duration_s, lap, strokes, w.get("swimDistance") or [])
         if sport == "swim" else
         {"lengths": None, "moving_s": None, "swolf": None, "swolf_gross": None,
          "pace_s_per_100m": None, "swolf_method": None})

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
                              "observations": 0, "sleep": 0}

    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO raw_payloads (body_sha256, body) VALUES (%s,%s) "
            "ON CONFLICT (body_sha256) DO NOTHING",
            (sha, json.dumps(payload)),
        )

        raw_m = BatchWriter(conn, "metrics", METRIC_COLS,
                            conflict=("name", "ts", "source"))
        obs = BatchWriter(conn, "observations", OBS_COLS,
                          conflict=("provider", "metric", "ts", "source"))
        sleep = BatchWriter(conn, "sleep_segments", SLEEP_COLS,
                            conflict=("provider", "started_at", "stage", "source"))
        wk = BatchWriter(conn, "workouts", WORKOUT_COLS, conflict=("id",))
        samples = BatchWriter(conn, "workout_samples", SAMPLE_COLS,
                              conflict=("workout_id", "metric", "ts"))

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
            cur = await conn.execute(
                "SELECT id, started_at FROM workouts WHERE started_at = ANY("
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

        for writer in (raw_m, obs, sleep, wk, samples):
            await writer.flush()

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
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


def _r(rows: list[dict], nd: int = 1) -> list[dict]:
    return [{k: (round(v, nd) if isinstance(v, float) else v) for k, v in row.items()}
            for row in rows]


@mcp.tool()
async def sync_status() -> dict:
    """Check whether the iPhone to database pipeline is alive and how fresh the data is.

    Call this first if other tools return suspiciously empty results. iOS only
    lets Health Auto Export run while the phone is unlocked, so gaps of a few
    hours are normal; gaps of days mean the automation stopped.
    """
    rows = await q("SELECT at, n_metrics, n_workouts FROM ingest_log ORDER BY at DESC LIMIT 1")
    if not rows:
        return {"status": "never_synced"}
    last = rows[0]["at"]
    age = (datetime.now(last.tzinfo) - last).total_seconds() / 3600
    return {"last_ingest": last.isoformat(), "age_hours": round(age, 1),
            "status": "ok" if age < 12 else "stale"}


@mcp.tool()
async def swim_sessions(since_days: int = 28, pool_length_m: float | None = None,
                        indoor_only: bool = True) -> list[dict]:
    """List swim sessions with pace and efficiency, newest first.

    SWOLF here excludes rest between sets, so interval and steady sessions are
    directly comparable; swolf_gross includes rest and is only useful for
    spotting how much of a session was spent standing at the wall.

    Filter by pool_length_m before comparing SWOLF across sessions: it sums
    strokes and seconds per length, so a 50m course yields structurally
    different numbers than 25m even when technique is unchanged.
    """
    return _r(await q(
        """
        SELECT id, started_at::date AS day, distance_m, duration_s, moving_s,
               pool_length_m, lengths, stroke_count, swim_cadence,
               swolf, swolf_gross, pace_s_per_100m, avg_hr, max_hr, hr_recovery
        FROM workouts
        WHERE sport = 'swim'
          AND started_at >= now() - make_interval(days => %s)
          AND (%s::boolean IS NOT TRUE OR is_indoor IS TRUE)
          AND (%s::float8 IS NULL OR pool_length_m = %s)
        ORDER BY started_at DESC
        """, (since_days, indoor_only, pool_length_m, pool_length_m)))


@mcp.tool()
async def session_detail(workout_id: str, bucket_minutes: int = 5) -> dict:
    """One session's summary plus its within-session shape, bucketed over time.

    Apple Watch does not export per-length splits, so this is the finest
    structure available: per-minute stroke rate and distance, aggregated into
    buckets. Falling distance with steady stroke rate suggests fatigue; gaps
    with near-zero distance are rests and mark set boundaries. Do not present
    these buckets as if they were 50m splits.
    """
    rows = await q("SELECT * FROM workouts WHERE id = %s", (workout_id,))
    if not rows:
        return {"error": "not found"}
    w = rows[0]
    w.pop("raw", None)
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
    return {"session": _r([w])[0], "buckets": _r(buckets)}


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
          WHERE sport = 'swim'
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
        WHERE sport = 'swim' AND distance_m BETWEEN 180 AND 420
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await pool.open()
    await apply_schema()
    async with mcp_app.lifespan(app):
        yield
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
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="not json")
    counts = await store(payload, hashlib.sha256(body).hexdigest())
    return {"ok": True, **counts}


@app.get("/health")
async def health() -> dict:
    """Target for the Home Assistant REST sensor. Alert when age_hours > 12."""
    rows = await q("SELECT at, n_metrics, n_workouts FROM ingest_log ORDER BY at DESC LIMIT 1")
    if not rows:
        return {"ok": True, "last_ingest": None, "age_hours": 999}
    last = rows[0]["at"]
    return {
        "ok": True,
        "last_ingest": last.isoformat(),
        "age_hours": round((datetime.now(last.tzinfo) - last).total_seconds() / 3600, 1),
        "last_metrics": rows[0]["n_metrics"],
        "last_workouts": rows[0]["n_workouts"],
    }


app.mount("/", mcp_app)
