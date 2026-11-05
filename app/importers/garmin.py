"""
Garmin Connect importer.

    python -m importers.garmin --login                 # once, interactive (MFA)
    python -m importers.garmin --probe --out DIR       # discover; writes nothing
    python -m importers.garmin --since 2026-10-20      # backfill
    python -m importers.garmin --days 3                # what the add-on does hourly
    python -m importers.garmin --replay                # re-parse archived raw only

Why this pulls from Garmin Connect directly instead of reading Garmin's data
back out of Apple Health: a Garmin pool swim's FIT file carries one `length`
message per length — active or idle, stroke count, stroke style. Idle lengths
are the rests at the wall, so active time is *measured*. None of that survives
the trip through HealthKit. (And the Garmin -> Apple Health sync is unreliable
anyway: it delivered 2026-10-25..27 and then nothing.)

There is no official personal API. `garminconnect` (>=0.3.20, which dropped the
dead `garth`) talks to the same endpoints the Connect app does and can break
without notice. That is why every response is archived raw before anything is
parsed — JSON into raw_payloads (kind='garmin'), FIT files onto /share and into
raw_files — and why --replay rebuilds everything from that archive alone.

Authentication: the password is used once, by --login, and never stored. The
library persists refreshable tokens to the token directory; every later run
resumes from those.

Data reality, verified against real payloads 2026-11-05 (see CLAUDE.md):
  * activity summaries give poolLength in CENTIMETRES (5000.0, factor 100).
    The FIT session's pool_length is metres. Pool length comes from the FIT.
  * the profile timezone (Europe/Paris on this account) is wrong; the
    startTimeLocal / startTimeGMT pair is right. See localtime.device_zone.
  * HRV is the overnight RMSSD (hrv_rmssd), not Apple's SDNN.
  * sleepLevels.activityLevel: 0 deep, 1 light, 2 rem, 3 awake.
  * a length is only as good as the swimmer's lap-button discipline: a rest
    without pressing it is folded into the next "active" length.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from canon import (GARMIN_SLEEP_LEVELS, canonical, garmin_sport, local_day, night_day,
                   to_canonical_value)
from settings import ARCHIVE_DIR, DSN, GARMIN_TOKEN_DIR, TZ

PROVIDER = "garmin"
SOURCE = "garmin"          # observations.source for Garmin's daily summaries
SEMICIRCLE = 180.0 / 2 ** 31
POLITE_S = 0.3             # pause between API calls; this is not our API


# ------------------------------------------------------------------- client --

def client(tokens: str = GARMIN_TOKEN_DIR):
    """A logged-in client resumed from saved tokens. Never prompts."""
    from garminconnect import Garmin
    g = Garmin()
    g.login(tokens)
    return g


def login(tokens: str = GARMIN_TOKEN_DIR) -> None:
    """Interactive first login. Saves tokens; the password goes nowhere."""
    from garminconnect import Garmin
    # The token file holds a refresh token — as good as the password for
    # reading the account, so owner-only from the moment it is written.
    d = Path(tokens).expanduser()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    g = Garmin(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
    old = os.umask(0o077)
    try:
        g.login(tokens)
    finally:
        os.umask(old)
    for f in d.iterdir():
        if f.is_file():
            f.chmod(0o600)
    print(f"logged in as {g.get_full_name()!r}; tokens saved to {tokens}")


# --------------------------------------------------------------------- FIT --

def fit_from_zip(blob: bytes) -> bytes:
    """ORIGINAL downloads arrive as a zip holding one .fit file."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".fit"))
        return z.read(name)


def fit_messages(fit: bytes):
    """Yield (message name, {field: value}) for every data message."""
    import fitdecode
    with fitdecode.FitReader(io.BytesIO(fit)) as r:
        for frame in r:
            if isinstance(frame, fitdecode.FitDataMessage):
                yield frame.name, {f.name: f.value for f in frame.fields}


@dataclass
class Fit:
    session: dict = field(default_factory=dict)
    lengths: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)


def parse_fit(fit: bytes) -> Fit:
    out = Fit()
    for name, f in fit_messages(fit):
        if name == "session" and not out.session:
            out.session = f
        elif name == "length":
            out.lengths.append(f)
        elif name == "record":
            out.records.append(f)
    return out


def _utc(v) -> datetime | None:
    """A FIT datetime, or one of Garmin's naive GMT strings, as aware UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):                       # epoch milliseconds
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace(" ", "T"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def active_lengths(fit: Fit, pool_m: float | None) -> list[tuple]:
    """Per-length splits, as workout_laps rows minus the workout id:
    (idx, started_at, duration_s, distance_m, stroke_style).

    Only `active` lengths. An `idle` length is Garmin's record of a rest the
    swimmer marked with the lap button — standing at the wall, not swimming —
    so it is excluded exactly as HAE's rests are. Numbered 1..n over active
    lengths so the count equals lengths swum.
    """
    out = []
    for l in fit.lengths:
        if l.get("length_type") != "active":
            continue
        start, dur = _utc(l.get("start_time")), _num(l.get("total_timer_time"))
        if start is None or not dur or dur <= 0:
            continue
        out.append((len(out) + 1, start, dur, pool_m, l.get("swim_stroke")))
    return out


def fit_samples(fit: Fit) -> list[tuple]:
    """Per-minute series, under the metric names the rest of the code reads.

    `heartRateData` from per-second records (Garmin's wrist sensor reads under
    water); `swimDistance` / `swimStroke` from active lengths, each length put
    in the minute it started. Rows are (metric, ts, qty, units, extra).
    """
    hr: dict[datetime, list[float]] = defaultdict(list)
    for r in fit.records:
        ts, v = _utc(r.get("timestamp")), _num(r.get("heart_rate"))
        if ts and v:
            hr[ts.replace(second=0, microsecond=0)].append(v)
    rows = [("heartRateData", m, sum(v) / len(v), "count/min",
             json.dumps({"Min": min(v), "Avg": sum(v) / len(v), "Max": max(v)}))
            for m, v in sorted(hr.items())]

    pool = _num(fit.session.get("pool_length"))
    dist: dict[datetime, float] = defaultdict(float)
    strokes: dict[datetime, float] = defaultdict(float)
    for l in fit.lengths:
        ts = _utc(l.get("start_time"))
        if l.get("length_type") != "active" or ts is None:
            continue
        m = ts.replace(second=0, microsecond=0)
        dist[m] += pool or 0.0
        strokes[m] += _num(l.get("total_strokes")) or 0.0
    if pool:
        rows += [("swimDistance", m, v, "m", None) for m, v in sorted(dist.items())]
    rows += [("swimStroke", m, v, "count", None) for m, v in sorted(strokes.items()) if v]
    return rows


def fit_route(fit: Fit) -> list[tuple]:
    """GPS fixes as (ts, lat, lon, ele_m, speed_ms) — the routes.write_route shape."""
    out = []
    for r in fit.records:
        lat, lon = r.get("position_lat"), r.get("position_long")
        if not isinstance(lat, int) or not isinstance(lon, int):
            continue
        out.append((_utc(r.get("timestamp")), lat * SEMICIRCLE, lon * SEMICIRCLE,
                    _num(r.get("enhanced_altitude") or r.get("altitude")),
                    _num(r.get("enhanced_speed") or r.get("speed"))))
    return out


# ----------------------------------------------------------------- workouts --

WORKOUT_COLS = (
    "id", "provider", "external_id", "sport", "is_indoor", "name_raw", "location_raw",
    "started_at", "ended_at", "local_day", "duration_s", "distance_m",
    "active_kcal", "total_kcal", "avg_hr", "min_hr", "max_hr",
    "pool_length_m", "stroke_count", "swim_cadence",
    "lengths", "moving_s", "swolf", "swolf_gross", "pace_s_per_100m", "swolf_method",
    "raw",
)


def workout_row(act: dict, fit: Fit | None, tz: str = TZ) -> tuple | None:
    """One Garmin activity summary (+ its FIT, when downloaded) onto WORKOUT_COLS.

    duration_s is ELAPSED time, as for Apple: "time in the building", rests
    included. Active time comes from the lengths.
    """
    from swim import derive_swim

    aid = act.get("activityId")
    start = _utc(act.get("startTimeGMT"))
    if not aid or start is None:
        return None
    sport, indoor = garmin_sport((act.get("activityType") or {}).get("typeKey"))
    s = fit.session if fit else {}

    elapsed = _num(act.get("elapsedDuration")) or _num(s.get("total_elapsed_time")) \
        or _num(act.get("duration"))
    distance = _num(act.get("distance")) or _num(s.get("total_distance"))
    total_kcal = _num(act.get("calories")) or _num(s.get("total_calories"))
    bmr = _num(act.get("bmrCalories"))
    active_kcal = total_kcal - bmr if total_kcal is not None and bmr is not None else None
    # Pool length from the FIT (metres). The summary's poolLength is in
    # centimetres; it is the fallback only, converted by its own factor.
    pool = _num(s.get("pool_length"))
    if pool is None and _num(act.get("poolLength")):
        factor = _num((act.get("unitOfPoolLength") or {}).get("factor")) or 100.0
        pool = act["poolLength"] / factor
    strokes = _num(s.get("total_strokes")) or _num(act.get("strokes"))

    d = {"lengths": None, "moving_s": None, "swolf": None, "swolf_gross": None,
         "pace_s_per_100m": None, "swolf_method": None}
    laps = active_lengths(fit, pool) if fit and sport == "swim" else []
    if sport == "swim" and laps:
        d = derive_swim(distance, elapsed, pool, strokes, [],
                        lap_count=len(laps), lap_total_s=sum(l[2] for l in laps))
        pool = pool or d.get("pool_length_m")
    elif sport == "swim" and indoor is not False:
        # A pool swim whose FIT has not been fetched yet. derive_swim would fall
        # back to the top-quartile estimate over an empty series, i.e. elapsed
        # time — a plausible wrong number. Left NULL; the next pull downloads
        # the FIT and fills it in.
        pass
    elif sport == "swim":
        # Open water: no lengths. Moving time is Garmin's own, from GPS speed.
        moving = _num(act.get("movingDuration"))
        if distance and moving:
            d["moving_s"] = moving
            d["pace_s_per_100m"] = moving / (distance / 100.0)

    return (
        f"{PROVIDER}:{aid}", PROVIDER, str(aid), sport, indoor,
        act.get("activityName"), act.get("locationName"),
        start, start + timedelta(seconds=elapsed) if elapsed else None,
        local_day(start, tz), elapsed, distance, active_kcal, total_kcal,
        _num(act.get("averageHR")) or _num(s.get("avg_heart_rate")),
        _num(act.get("minHR")) or _num(s.get("min_heart_rate")),
        _num(act.get("maxHR")) or _num(s.get("max_heart_rate")),
        pool if sport == "swim" else None, strokes if sport == "swim" else None,
        _num(s.get("avg_cadence")) if sport == "swim" else None,
        d["lengths"], d["moving_s"], d["swolf"], d["swolf_gross"],
        d["pace_s_per_100m"], d["swolf_method"],
        json.dumps(act, default=str),
    )


# -------------------------------------------------------------------- daily --

def _day_ts(day: str, tz: str) -> datetime:
    """A daily summary is stamped at local midnight, so local_day is that day."""
    return datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))


def _obs(raw_name: str, day: str, value, unit: str | None, tz: str) -> tuple | None:
    metric = canonical(PROVIDER, raw_name)
    v = _num(value)
    if metric is None or v is None or v < 0:          # Garmin uses -1/-2 for "none"
        return None
    conv = to_canonical_value(metric, v, unit)
    if not conv or conv[0] is None:
        return None
    ts = _day_ts(day, tz)
    return (PROVIDER, metric, ts, local_day(ts, tz), conv[0], conv[1], SOURCE)


_STATS_FIELDS = ("totalSteps", "totalDistanceMeters", "floorsAscended",
                 "activeKilocalories", "bmrKilocalories", "restingHeartRate",
                 "avgWakingRespirationValue", "bodyBatteryHighestValue",
                 "bodyBatteryLowestValue", "averageStressLevel")


def daily_obs(endpoint: str, params: dict, data, tz: str = TZ) -> list[tuple]:
    """observations rows from one archived daily response."""
    day = params.get("date")
    rows: list[tuple | None] = []
    if endpoint == "stats" and isinstance(data, dict) and day:
        rows += [_obs(k, day, data.get(k), None, tz) for k in _STATS_FIELDS]
        mod, vig = _num(data.get("moderateIntensityMinutes")), \
            _num(data.get("vigorousIntensityMinutes"))
        if mod is not None or vig is not None:
            # Garmin's own definition: vigorous minutes count double.
            rows.append(_obs("intensityMinutes", day, (mod or 0) + 2 * (vig or 0), None, tz))
    elif endpoint == "hrv" and isinstance(data, dict) and day:
        rows.append(_obs("lastNightAvg", day,
                         (data.get("hrvSummary") or {}).get("lastNightAvg"), None, tz))
    elif endpoint == "spo2" and isinstance(data, dict) and day:
        rows.append(_obs("averageSpO2", day, data.get("averageSpO2"), "%", tz))
    elif endpoint == "max_metrics" and isinstance(data, list) and day:
        for m in data:
            g = (m or {}).get("generic") or {}
            rows.append(_obs("vo2MaxValue", day,
                             g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue"), None, tz))
    elif endpoint == "body_comp" and isinstance(data, dict):
        for w in data.get("dateWeightList") or []:
            ts = _utc(w.get("date") or w.get("samplePk"))
            if ts is None:
                continue
            d = ts.astimezone(ZoneInfo(tz)).date().isoformat()
            # Garmin keeps weight in grams.
            for name, unit in (("weight", "g"), ("bodyFat", "%")):
                r = _obs(name, d, w.get(name), unit, tz)
                if r:
                    rows.append(r[:2] + (ts, r[3]) + r[4:])
    return [r for r in rows if r]


def sleep_rows(data, tz: str = TZ) -> list[tuple]:
    """sleep_segments rows (provider, start, end, stage, source, local_day)."""
    if not isinstance(data, dict):
        return []
    rows = []
    dto = data.get("dailySleepDTO") or {}
    s, e = _utc(dto.get("sleepStartTimestampGMT")), _utc(dto.get("sleepEndTimestampGMT"))
    if s and e and e > s:
        rows.append((PROVIDER, s, e, "inbed", SOURCE, night_day(s, tz)))
    for lvl in data.get("sleepLevels") or []:
        stage = GARMIN_SLEEP_LEVELS.get(int(lvl.get("activityLevel", -1)))
        a, b = _utc(lvl.get("startGMT")), _utc(lvl.get("endGMT"))
        if stage and a and b and b > a:
            rows.append((PROVIDER, a, b, stage, SOURCE, night_day(a, tz)))
    return rows


# -------------------------------------------------------------------- fetch --

@dataclass
class Bundle:
    """Everything one pull fetched, before any parsing."""
    responses: list[tuple[str, dict, object]] = field(default_factory=list)
    fits: dict[str, bytes] = field(default_factory=dict)       # activityId -> FIT


DAILY = {
    "stats":       lambda g, d: g.get_stats(d),
    "hrv":         lambda g, d: g.get_hrv_data(d),
    "sleep":       lambda g, d: g.get_sleep_data(d),
    "spo2":        lambda g, d: g.get_spo2_data(d),
    "max_metrics": lambda g, d: g.get_max_metrics(d),
}


def fetch(g, since: date, until: date, have_fit: set[str]) -> Bundle:
    """Pull one window. Blocking (the library is synchronous) — the server runs
    this in a thread. A failing endpoint is recorded and skipped, so one broken
    call does not throw away the rest of the window."""
    b = Bundle()

    def call(endpoint: str, params: dict, fn):
        try:
            data = fn()
        except Exception as e:                                  # noqa: BLE001
            print(f"  garmin {endpoint} {params}: {type(e).__name__}: {e}", flush=True)
            return None
        b.responses.append((endpoint, params, data))
        time.sleep(POLITE_S)
        return data

    acts = call("activities", {"from": since.isoformat(), "to": until.isoformat()},
                lambda: g.get_activities_by_date(since.isoformat(), until.isoformat())) or []
    for a in acts:
        aid = str(a.get("activityId"))
        if aid in have_fit:
            continue
        try:
            blob = g.download_activity(aid, dl_fmt=g.ActivityDownloadFormat.ORIGINAL)
            b.fits[aid] = fit_from_zip(blob)
        except Exception as e:                                  # noqa: BLE001
            print(f"  garmin FIT {aid}: {type(e).__name__}: {e}", flush=True)
        time.sleep(POLITE_S)

    d = since
    while d <= until:
        for name, fn in DAILY.items():
            call(name, {"date": d.isoformat()}, lambda fn=fn, d=d: fn(g, d.isoformat()))
        d += timedelta(days=1)
    call("body_comp", {"from": since.isoformat(), "to": until.isoformat()},
         lambda: g.get_body_composition(since.isoformat(), until.isoformat()))
    return b


# -------------------------------------------------------------------- store --

def _body(endpoint: str, params: dict, data) -> tuple[str, str]:
    body = json.dumps({"endpoint": endpoint, "params": params, "data": data},
                      default=str, sort_keys=True, ensure_ascii=False)
    return body, hashlib.sha256(body.encode()).hexdigest()


def fit_dir(archive: Path = ARCHIVE_DIR) -> Path:
    return archive / "garmin" / "fit"


async def archive(conn, b: Bundle, archive_dir: Path = ARCHIVE_DIR) -> None:
    """Raw first, committed on its own — a parser bug must not lose the pull."""
    from db import register_raw_file
    for endpoint, params, data in b.responses:
        body, sha = _body(endpoint, params, data)
        await conn.execute(
            "INSERT INTO raw_payloads (body_sha256, body, kind) VALUES (%s, %s, 'garmin')"
            " ON CONFLICT (body_sha256) DO NOTHING", (sha, body))
    if b.fits:
        fit_dir(archive_dir).mkdir(parents=True, exist_ok=True)
    for aid, fit in b.fits.items():
        path = fit_dir(archive_dir) / f"{aid}.fit"
        path.write_bytes(fit)
        await register_raw_file(conn, str(path), hashlib.sha256(fit).hexdigest(),
                                "garmin_fit", len(fit), note=aid)
    await conn.commit()


async def store(conn, b: Bundle, tz: str = TZ, archive_dir: Path = ARCHIVE_DIR) -> dict:
    """Parse a bundle into workouts / laps / samples / routes / observations /
    sleep. Idempotent: every write is an upsert, and a workout's laps are
    replaced as a set."""
    import localtime
    from db import BatchWriter
    from derive import mark_superseded
    from importers.routes import route_writers, write_route

    counts = Counter()
    wk = BatchWriter(conn, "workouts", WORKOUT_COLS, conflict=("id",),
                     coalesce=("pool_length_m", "stroke_count", "swim_cadence"))
    laps = BatchWriter(conn, "workout_laps",
                       ("workout_id", "idx", "started_at", "duration_s", "distance_m",
                        "stroke_style"), conflict=("workout_id", "idx"))
    samples = BatchWriter(conn, "workout_samples",
                          ("workout_id", "metric", "ts", "qty", "units", "extra"),
                          conflict=("workout_id", "metric", "ts"))
    obs = BatchWriter(conn, "observations",
                      ("provider", "metric", "ts", "local_day", "value", "unit", "source"),
                      conflict=("provider", "metric", "ts", "source"))
    sleep = BatchWriter(conn, "sleep_segments",
                        ("provider", "started_at", "ended_at", "stage", "source", "local_day"),
                        conflict=("provider", "started_at", "stage", "source"))
    rpts, rsum = route_writers(conn)

    # The latest summary per activity wins: a later pull may carry an edit.
    acts: dict[str, dict] = {}
    for endpoint, params, data in b.responses:
        if endpoint == "activities" and isinstance(data, list):
            for a in data:
                acts[str(a.get("activityId"))] = a
        elif endpoint == "sleep":
            for r in sleep_rows(data, tz):
                await sleep.add(r)
                counts["sleep"] += 1
        else:
            for r in daily_obs(endpoint, params, data, tz):
                await obs.add(r)
                counts["observations"] += 1

    # FITs: the ones just fetched, else the archived copy on disk.
    for aid, a in acts.items():
        fit_bytes = b.fits.get(aid)
        if fit_bytes is None:
            p = fit_dir(archive_dir) / f"{aid}.fit"
            fit_bytes = p.read_bytes() if p.exists() else None
        fit = parse_fit(fit_bytes) if fit_bytes else None
        row = workout_row(a, fit, tz)
        if not row:
            continue
        await wk.add(row)
        await wk.flush()                       # children reference the parent
        counts["workouts"] += 1
        if fit is None:
            continue
        wid = row[0]
        if row[3] == "swim":
            await conn.execute("DELETE FROM workout_laps WHERE workout_id = %s", (wid,))
            for lap in active_lengths(fit, row[17]):
                await laps.add((wid, *lap))
                counts["laps"] += 1
        for metric, ts, q, u, extra in fit_samples(fit):
            await samples.add((wid, metric, ts, q, u, extra))
            counts["samples"] += 1
        route = fit_route(fit)
        if route:
            counts["route_points"] += await write_route(conn, rpts, rsum, wid, "garmin_fit",
                                                        route)

    for w in (wk, laps, samples, obs, sleep, rpts, rsum):
        await w.flush()
    if acts:
        await localtime.resolve(conn)
    counts["superseded_changed"] = await mark_superseded(conn)
    await conn.execute(
        "INSERT INTO ingest_log (n_metrics, n_workouts, n_samples, provider, note)"
        " VALUES (%s, %s, %s, 'garmin', %s)",
        (counts["observations"], counts["workouts"], counts["samples"],
         json.dumps(dict(counts))))
    await conn.commit()
    return dict(counts)


async def have_fits(conn) -> set[str]:
    cur = await conn.execute("SELECT note FROM raw_files WHERE kind = 'garmin_fit'")
    return {r[0] for r in await cur.fetchall() if r[0]}


async def pull(conn, since: date, until: date, g=None, tz: str = TZ,
               archive_dir: Path = ARCHIVE_DIR) -> dict:
    """Fetch, archive, store. `g` is a logged-in client; made if not given."""
    have = await have_fits(conn)
    if g is None:
        g = await asyncio.to_thread(client)
    b = await asyncio.to_thread(fetch, g, since, until, have)
    await archive(conn, b, archive_dir)
    return await store(conn, b, tz, archive_dir)


async def replay(conn, tz: str = TZ, archive_dir: Path = ARCHIVE_DIR) -> dict:
    """Rebuild every Garmin row from the raw archive alone — no network."""
    cur = await conn.execute(
        "SELECT body FROM raw_payloads WHERE kind = 'garmin' ORDER BY received_at, id")
    b = Bundle()
    for (body,) in await cur.fetchall():
        b.responses.append((body["endpoint"], body["params"], body["data"]))
    return await store(conn, b, tz, archive_dir)


# ------------------------------------------------------------------- probe --

def describe_fit(fit: bytes) -> dict:
    """What a FIT file holds: message counts, and samples of the swim messages."""
    counts: Counter = Counter()
    first: dict[str, dict] = {}
    lengths: list[dict] = []
    for name, fields in fit_messages(fit):
        counts[name] += 1
        first.setdefault(name, fields)
        if name == "length":
            lengths.append(fields)
    return {"messages": dict(counts),
            "session": first.get("session"),
            "activity": first.get("activity"),
            "first_length": lengths[0] if lengths else None,
            "length_types": dict(Counter(str(l.get("length_type")) for l in lengths)),
            "swim_strokes": dict(Counter(str(l.get("swim_stroke")) for l in lengths)),
            "first_record": first.get("record"),
            "first_lap": first.get("lap")}


PROBE_DAILY = {
    **DAILY,
    "rhr":           lambda g, d: g.get_rhr_day(d),
    "stress":        lambda g, d: g.get_stress_data(d),
    "body_battery":  lambda g, d: g.get_body_battery(d),
    "body_comp":     lambda g, d: g.get_body_composition(d),
    "respiration":   lambda g, d: g.get_respiration_data(d),
    "intensity":     lambda g, d: g.get_intensity_minutes_data(d),
    "css":           lambda g, d: g.get_critical_swim_speed(d),
}


def _save(out: Path, name: str, data) -> None:
    (out / name).write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _shape(v, depth: int = 0):
    if isinstance(v, dict):
        if depth >= 2:
            return f"{{{len(v)} keys}}"
        return {k: _shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [f"{len(v)} items", _shape(v[0], depth + 1)] if v else []
    return type(v).__name__


def probe(out: Path, days: int, tokens: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    g = client(tokens)
    print(f"user timezone: {g.get_user_timezone()}")
    end = date.today()
    start = end - timedelta(days=days)

    acts = g.get_activities_by_date(start.isoformat(), end.isoformat())
    _save(out, "activities.json", acts)
    print(f"\n{len(acts)} activities {start} → {end}")
    for a in acts:
        print(f"  {a.get('activityId'):>12}  {a['activityType']['typeKey']:<22} "
              f"{a.get('startTimeGMT')}  local {a.get('startTimeLocal')}  "
              f"{a.get('distance') or 0:.0f} m  pool {a.get('poolLength')}")

    wanted: dict[str, dict] = {}
    for a in acts:
        wanted.setdefault(a["activityType"]["typeKey"], a)
    for key, a in wanted.items():
        aid = a["activityId"]
        try:
            fit = fit_from_zip(g.download_activity(
                aid, dl_fmt=g.ActivityDownloadFormat.ORIGINAL))
        except Exception as e:                                  # noqa: BLE001
            print(f"\n[{key}] FIT download failed: {e}")
            continue
        (out / f"{key}_{aid}.fit").write_bytes(fit)
        desc = describe_fit(fit)
        _save(out, f"{key}_{aid}.fit.json", desc)
        print(f"\n[{key}] {aid}: {len(fit):,} bytes\n  messages: {desc['messages']}")
        if desc["first_length"]:
            print(f"  length types: {desc['length_types']}  strokes: {desc['swim_strokes']}")

    day = (end - timedelta(days=1)).isoformat()
    print(f"\ndaily endpoints for {day}:")
    for name, call in PROBE_DAILY.items():
        try:
            data = call(g, day)
        except Exception as e:                                  # noqa: BLE001
            print(f"  {name:<14} FAILED {type(e).__name__}: {e}")
            continue
        _save(out, f"daily_{name}.json", data)
        print(f"  {name:<14} {json.dumps(_shape(data), ensure_ascii=False)[:300]}")
    print(f"\nsaved to {out}")


# --------------------------------------------------------------------- cli --

async def _run(args) -> None:
    import psycopg
    async with await psycopg.AsyncConnection.connect(args.dsn) as conn:
        if args.replay:
            counts = await replay(conn, archive_dir=args.archive_dir)
        else:
            until = date.today()
            since = args.since or until - timedelta(days=args.days)
            g = client(args.tokens)
            counts = await pull(conn, since, until, g, archive_dir=args.archive_dir)
    print(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Garmin Connect importer.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--login", action="store_true", help="interactive first login")
    g.add_argument("--probe", action="store_true", help="discover; write nothing to the DB")
    g.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="pull from this day to today")
    g.add_argument("--days", type=int, help="pull the last N days")
    g.add_argument("--replay", action="store_true",
                   help="re-parse the archived raw responses; no network")
    ap.add_argument("--out", type=Path,
                    default=ARCHIVE_DIR / "garmin" / "probe" / date.today().isoformat())
    ap.add_argument("--tokens", default=GARMIN_TOKEN_DIR)
    ap.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR)
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args(argv)

    if args.login:
        login(args.tokens)
    elif args.probe:
        probe(args.out, 14, args.tokens)
    else:
        asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
