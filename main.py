"""
Longcourse — Apple Health ingest + MCP, one process.

Routes:
  POST /ingest   Health Auto Export REST automation target (bearer auth)
  GET  /health   freshness probe for the Home Assistant watchdog sensor
  ANY  /mcp      MCP over HTTP for Claude Code / claude.ai

Two invariants worth keeping:
  1. The raw body is archived before anything is parsed. If the parser is wrong
     (and it will be, at least once), we replay instead of losing data.
  2. Every write is an upsert. HAE resends overlapping windows on every run.
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
# Supervisor writes add-on options here. Fall back to env vars so the same
# image can be run under plain docker for local testing.
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

pool: AsyncConnectionPool | None = None


# ----------------------------------------------------------------- parsing --

# HAE emits "2024-02-06 07:00:00 -0800" and occasionally ISO-8601.
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
    """HAE wraps most numbers as {"qty": x, "units": "..."} but not always."""
    if isinstance(obj, dict):
        v = obj.get("qty")
        return float(v) if isinstance(v, (int, float)) else None
    return float(obj) if isinstance(obj, (int, float)) else None


def units(obj: Any) -> str | None:
    return obj.get("units") if isinstance(obj, dict) else None


def to_meters(q_: float | None, u: str | None) -> float | None:
    """Normalize to metres at the edge. Never store mixed units."""
    if q_ is None:
        return None
    f = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}
    return q_ * f.get((u or "m").lower(), 1.0)


def dig(d: dict, *path: str) -> Any:
    """Tolerant lookup: v2 nests, v1 flattens. Try both."""
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None and path:
        cur = d.get(path[-1])
    return cur


# Candidate keys for per-length data. HAE v2 documents lapLength/strokeStyle at
# the workout level; whether it also ships an array of lengths is the open
# question this pipeline is built to answer either way.
LENGTH_KEYS = ("lengths", "laps", "splits", "swimLengths", "intervals")


def extract_lengths(w: dict) -> list[dict]:
    for key in LENGTH_KEYS:
        arr = w.get(key)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            return arr
    return []


def workout_row(w: dict) -> tuple | None:
    wid = w.get("id") or w.get("uuid")
    start = parse_dt(w.get("start") or w.get("startDate"))
    if not wid or not start:
        return None

    dist = w.get("distance")
    swim = w.get("swimmingMetrics") if isinstance(w.get("swimmingMetrics"), dict) else w
    lap = swim.get("lapLength") if isinstance(swim, dict) else None
    g = swim.get if isinstance(swim, dict) else (lambda *_: None)

    return (
        str(wid),
        w.get("name") or w.get("workoutActivityType") or "Unknown",
        start,
        parse_dt(w.get("end") or w.get("endDate")),
        qty(w.get("duration")),
        to_meters(qty(dist), units(dist)),
        qty(dig(w, "activeEnergyBurned")),
        qty(dig(w, "heartRateData", "average")) or qty(w.get("avgHeartRate")),
        qty(dig(w, "heartRateData", "max")) or qty(w.get("maxHeartRate")),
        to_meters(qty(lap), units(lap)),
        g("strokeStyle"),
        qty(g("swolfScore")),
        qty(g("totalSwimmingStrokeCount")),
        qty(g("swimCadence")),
        g("salinity"),
        json.dumps(w),
    )


def length_rows(wid: str, arr: list[dict]) -> list[tuple]:
    rows = []
    for i, l in enumerate(arr):
        d = l.get("distance")
        rows.append((
            wid, i,
            parse_dt(l.get("start") or l.get("date")),
            qty(l.get("duration")),
            to_meters(qty(d), units(d)),
            l.get("strokeStyle"),
            qty(l.get("strokeCount") or l.get("totalSwimmingStrokeCount")),
            qty(l.get("swolfScore") or l.get("swolf")),
            qty(l.get("avgHeartRate") or dig(l, "heartRateData", "average")),
            json.dumps(l),
        ))
    return rows


async def store(payload: dict, sha: str) -> dict:
    data = payload.get("data", payload)
    n_m = n_w = n_l = 0

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
                "INSERT INTO workouts (id,name,started_at,ended_at,duration_s,"
                " distance_m,active_kcal,avg_hr,max_hr,pool_length_m,stroke_style,"
                " swolf,stroke_count,swim_cadence,salinity,raw)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET"
                " ended_at=EXCLUDED.ended_at, duration_s=EXCLUDED.duration_s,"
                " distance_m=EXCLUDED.distance_m, active_kcal=EXCLUDED.active_kcal,"
                " avg_hr=EXCLUDED.avg_hr, max_hr=EXCLUDED.max_hr,"
                " pool_length_m=EXCLUDED.pool_length_m, stroke_style=EXCLUDED.stroke_style,"
                " swolf=EXCLUDED.swolf, stroke_count=EXCLUDED.stroke_count,"
                " swim_cadence=EXCLUDED.swim_cadence, salinity=EXCLUDED.salinity,"
                " raw=EXCLUDED.raw, updated_at=now()",
                row,
            )
            n_w += 1
            for lr in length_rows(row[0], extract_lengths(w)):
                await conn.execute(
                    "INSERT INTO swim_lengths (workout_id,idx,started_at,duration_s,"
                    " distance_m,stroke_style,stroke_count,swolf,avg_hr,raw)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (workout_id, idx) DO UPDATE SET"
                    " duration_s=EXCLUDED.duration_s, distance_m=EXCLUDED.distance_m,"
                    " stroke_style=EXCLUDED.stroke_style, stroke_count=EXCLUDED.stroke_count,"
                    " swolf=EXCLUDED.swolf, avg_hr=EXCLUDED.avg_hr, raw=EXCLUDED.raw",
                    lr,
                )
                n_l += 1

        await conn.execute(
            "INSERT INTO ingest_log (n_metrics,n_workouts,n_lengths) VALUES (%s,%s,%s)",
            (n_m, n_w, n_l),
        )

    return {"metrics": n_m, "workouts": n_w, "lengths": n_l}


# --------------------------------------------------------------------- MCP --
# Deliberately not a SQL passthrough. Each tool answers a coaching question and
# returns a small, pre-aggregated payload. A generic query tool would let the
# agent pull thousands of raw rows and burn the context window on data it then
# has to re-summarise itself.

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
async def swim_sessions(since_days: int = 28, pool_length_m: float | None = None) -> list[dict]:
    """List swim sessions with pace and efficiency, newest first.

    Filter by pool_length_m before comparing SWOLF across sessions: SWOLF sums
    strokes and seconds per length, so a 50m course yields structurally
    different numbers than 25m even when technique is unchanged.
    """
    return _r(await q(
        """
        SELECT id, started_at::date AS day, distance_m, duration_s, pool_length_m,
               stroke_style, swolf, stroke_count, swim_cadence, avg_hr, max_hr,
               CASE WHEN distance_m > 0 THEN duration_s / (distance_m/100.0) END
                 AS pace_s_per_100m
        FROM workouts
        WHERE name ILIKE '%%swim%%'
          AND started_at >= now() - make_interval(days => %s)
          AND (%s::float8 IS NULL OR pool_length_m = %s)
        ORDER BY started_at DESC
        """, (since_days, pool_length_m, pool_length_m)))


@mcp.tool()
async def session_detail(workout_id: str) -> dict:
    """Full breakdown of one swim, including per-length splits when available.

    If lengths comes back empty this export carries only session-level
    aggregates. Say so plainly rather than inferring set structure.
    """
    rows = await q("SELECT * FROM workouts WHERE id = %s", (workout_id,))
    if not rows:
        return {"error": "not found"}
    w = rows[0]
    w.pop("raw", None)
    lens = await q(
        "SELECT idx,duration_s,distance_m,stroke_style,stroke_count,swolf,avg_hr "
        "FROM swim_lengths WHERE workout_id=%s ORDER BY idx", (workout_id,))
    return {"session": _r([w])[0], "lengths": _r(lens), "n_lengths": len(lens)}


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
          SELECT started_at, distance_m, duration_s, swolf,
                 CASE WHEN started_at >= now() - make_interval(days => %s)
                      THEN 'current' ELSE 'previous' END AS block
          FROM workouts
          WHERE name ILIKE '%%swim%%'
            AND started_at >= now() - make_interval(days => %s))
        SELECT block, COUNT(*) AS sessions, SUM(distance_m) AS distance_m,
               SUM(duration_s) AS duration_s, AVG(swolf) AS avg_swolf,
               SUM(duration_s)/NULLIF(SUM(distance_m)/100.0,0) AS pace_s_per_100m
        FROM s GROUP BY block
        """, (block_days, block_days * 2))
    return {r["block"]: _r([r])[0] for r in rows}


@mcp.tool()
async def css_inputs() -> dict:
    """Fastest recent 400m and 200m efforts — the inputs for a Critical Swim Speed test.

    CSS = (400 - 200) / (t400 - t200). Without a recent test the training zones
    are guesswork, so if the best efforts here are older than about six weeks,
    recommend re-testing before writing a plan.
    """
    rows = await q(
        """
        SELECT started_at::date AS day, distance_m, duration_s
        FROM workouts
        WHERE name ILIKE '%%swim%%' AND distance_m BETWEEN 180 AND 420
        ORDER BY duration_s/NULLIF(distance_m,0) ASC LIMIT 10
        """)
    return {"candidates": _r(rows),
            "note": "Verify these were maximal time trials, not warmup segments."}


# --------------------------------------------------------------------- app --

mcp_app = mcp.http_app(path="/mcp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await pool.open()
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
