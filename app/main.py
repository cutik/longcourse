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
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# ------------------------------------------------------------------ options --
OPTS: dict[str, Any] = {}
_opts_file = Path("/data/options.json")
if _opts_file.exists():
    OPTS = json.loads(_opts_file.read_text())


def opt(key: str, default: Any = None) -> Any:
    return OPTS.get(key) or os.getenv(key.upper(), default)


DSN = (
    f"postgresql://{opt('db_user')}:{opt('db_password')}"
    f"@{opt('db_host')}:{opt('db_port', 5432)}/{opt('db_name')}"
)
TOKEN = opt("ingest_token")
SCHEMA = Path(__file__).with_name("schema.sql")

pool: AsyncConnectionPool | None = None


# ----------------------------------------------------------------- parsing --

_HAE_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?$")


def parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    m = _HAE_DT.match(v.strip())
    if m:
        d, t, off = m.groups()
        return datetime.strptime(f"{d} {t} {off or '+0000'}", "%Y-%m-%d %H:%M:%S %z")
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def qty(obj: Any) -> float | None:
    if isinstance(obj, dict):
        v = obj.get("qty")
        return float(v) if isinstance(v, (int, float)) else None
    return float(obj) if isinstance(obj, (int, float)) else None


def units(obj: Any) -> str | None:
    return obj.get("units") if isinstance(obj, dict) else None


_DIST = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}


def to_meters(obj: Any) -> float | None:
    q, u = qty(obj), (units(obj) or "m").lower()
    return None if q is None else q * _DIST.get(u, 1.0)


def pool_length_m(obj: Any) -> float | None:
    """Repair HAE's lapLength unit bug.

    A 50m pool exports as {"units":"m","qty":0.05} — the number is kilometres
    but the label says metres. No real pool is under a metre, so a sub-1 value
    tagged as metres is unambiguously the km case.
    """
    q = qty(obj)
    if q is None or q <= 0:
        return None
    u = (units(obj) or "m").lower()
    if u == "m" and q < 1:
        q *= 1000.0
    elif u != "m":
        q *= _DIST.get(u, 1.0)
    return q if 5 <= q <= 100 else None


def kcal(obj: Any) -> float | None:
    """HAE reports workout energy in kJ despite the field names."""
    q, u = qty(obj), (units(obj) or "").lower()
    if q is None:
        return None
    return q / 4.184 if u == "kj" else q


# Structural swim signals — present regardless of interface language.
SWIM_KEYS = ("swimDistance", "swimStroke", "swimCadence",
             "totalSwimmingStrokeCount", "lapLength")


def classify(w: dict) -> str:
    """Derive sport without touching the localised name.

    HAE returns name and location in the phone's language ("Басейн Плавання"),
    so any ILIKE '%swim%' filter silently returns nothing for non-English
    users. Structure is stable; strings are not.
    """
    if any(k in w for k in SWIM_KEYS):
        return "swim"
    if "stepCount" in w or "flightsClimbed" in w:
        return "walk"
    if "runningPower" in w or "runningSpeed" in w or "groundContactTime" in w:
        return "run"
    return "other"


# Time-series keys we lift into workout_samples.
SERIES_KEYS = ("swimStroke", "swimDistance", "heartRateData", "activeEnergy",
               "basalEnergy", "heartRateRecovery", "stepCount", "speed")


def moving_seconds(series: list[dict], min_qty: float = 5.0) -> float | None:
    """Active time, estimated from per-bucket swim distance.

    Buckets carrying almost no distance are rests between sets. Excluding them
    is what makes SWOLF comparable between a steady swim and an interval
    session — gross duration would punish the interval session for resting.
    Bucket width is inferred from the gaps rather than assumed to be 60s.
    """
    pts = sorted(
        [(parse_dt(p.get("date")), qty(p)) for p in series if parse_dt(p.get("date"))],
        key=lambda x: x[0],
    )
    if len(pts) < 2:
        return None
    gaps = [(pts[i + 1][0] - pts[i][0]).total_seconds() for i in range(len(pts) - 1)]
    width = sorted(gaps)[len(gaps) // 2]
    if width <= 0:
        return None
    return sum(width for _, q in pts if (q or 0) >= min_qty)


def workout_row(w: dict) -> tuple | None:
    wid = w.get("id") or w.get("uuid")
    start = parse_dt(w.get("start"))
    if not wid or not start:
        return None

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

    # Derived swim figures. Computed here rather than in queries so every
    # consumer sees the same numbers and the same rest-handling.
    lengths = moving_s = swolf = swolf_gross = pace = None
    if sport == "swim" and lap and distance_m and distance_m > 0:
        lengths = max(1, round(distance_m / lap))
        moving_s = moving_seconds(w.get("swimDistance") or []) or duration_s
        if strokes:
            spl = strokes / lengths
            if moving_s:
                swolf = moving_s / lengths + spl
            if duration_s:
                swolf_gross = duration_s / lengths + spl
        if moving_s:
            pace = moving_s / (distance_m / 100.0)

    return (
        str(wid), sport, w.get("isIndoor"), w.get("name"), w.get("location"),
        start, parse_dt(w.get("end")), duration_s, distance_m,
        kcal(w.get("activeEnergyBurned")), kcal(w.get("totalEnergy")),
        qty(w.get("avgHeartRate")) or qty(hr.get("avg")),
        qty(hr.get("min")), qty(w.get("maxHeartRate")) or qty(hr.get("max")),
        hrr, lap, strokes, qty(w.get("swimCadence")),
        lengths, moving_s, swolf, swolf_gross, pace,
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


async def store(payload: dict, sha: str) -> dict:
    data = payload.get("data", payload)
    n_m = n_w = n_s = 0

    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO raw_payloads (body_sha256, body) VALUES (%s,%s) "
            "ON CONFLICT (body_sha256) DO NOTHING",
            (sha, json.dumps(payload)),
        )

        for m in data.get("metrics") or []:
            name, u = m.get("name"), m.get("units")
            for p in m.get("data") or []:
                ts = parse_dt(p.get("date"))
                if not name or not ts:
                    continue
                extra = {k: v for k, v in p.items()
                         if k not in {"date", "qty", "source", "units"}} or None
                await conn.execute(
                    "INSERT INTO metrics (name, ts, source, qty, units, extra) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (name, ts, source) DO UPDATE SET "
                    " qty=EXCLUDED.qty, units=EXCLUDED.units, extra=EXCLUDED.extra",
                    (name, ts, p.get("source") or "", qty(p), u,
                     json.dumps(extra) if extra else None),
                )
                n_m += 1

        for w in data.get("workouts") or []:
            row = workout_row(w)
            if not row:
                continue
            await conn.execute(
                "INSERT INTO workouts (id,sport,is_indoor,name_raw,location_raw,"
                " started_at,ended_at,duration_s,distance_m,active_kcal,total_kcal,"
                " avg_hr,min_hr,max_hr,hr_recovery,pool_length_m,stroke_count,"
                " swim_cadence,lengths,moving_s,swolf,swolf_gross,pace_s_per_100m,raw)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "         %s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET"
                " sport=EXCLUDED.sport, is_indoor=EXCLUDED.is_indoor,"
                " name_raw=EXCLUDED.name_raw, location_raw=EXCLUDED.location_raw,"
                " ended_at=EXCLUDED.ended_at, duration_s=EXCLUDED.duration_s,"
                " distance_m=EXCLUDED.distance_m, active_kcal=EXCLUDED.active_kcal,"
                " total_kcal=EXCLUDED.total_kcal, avg_hr=EXCLUDED.avg_hr,"
                " min_hr=EXCLUDED.min_hr, max_hr=EXCLUDED.max_hr,"
                " hr_recovery=EXCLUDED.hr_recovery,"
                " pool_length_m=EXCLUDED.pool_length_m,"
                " stroke_count=EXCLUDED.stroke_count,"
                " swim_cadence=EXCLUDED.swim_cadence, lengths=EXCLUDED.lengths,"
                " moving_s=EXCLUDED.moving_s, swolf=EXCLUDED.swolf,"
                " swolf_gross=EXCLUDED.swolf_gross,"
                " pace_s_per_100m=EXCLUDED.pace_s_per_100m,"
                " raw=EXCLUDED.raw, updated_at=now()",
                row,
            )
            n_w += 1
            for sr in sample_rows(row[0], w):
                await conn.execute(
                    "INSERT INTO workout_samples (workout_id,metric,ts,qty,units,extra)"
                    " VALUES (%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (workout_id,metric,ts) DO UPDATE SET"
                    " qty=EXCLUDED.qty, units=EXCLUDED.units, extra=EXCLUDED.extra",
                    sr,
                )
                n_s += 1

        await conn.execute(
            "INSERT INTO ingest_log (n_metrics,n_workouts,n_samples) VALUES (%s,%s,%s)",
            (n_m, n_w, n_s),
        )

    return {"metrics": n_m, "workouts": n_w, "samples": n_s}


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
    if authorization != f"Bearer {TOKEN}":
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
