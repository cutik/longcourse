"""
Parser and normalisation checks. Plain asserts, no test framework:

    python3 tests/test_parsers.py

The project had no tests at all, and the failure mode that matters here is not a
crash — it is a plausible-looking wrong number. A lap length read as 0.05 m, kJ
counted as kcal, a 23:00 session filed under tomorrow, an oxygen saturation of
0.97 plotted as 0.97%: every one of those renders a clean chart of nonsense.
These lock the conversions that were expensive to get right.

Nothing here touches Postgres. Database-shaped work is verified by the SQL
checks in CLAUDE.md after a replay.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))

# psycopg is only needed by the import path, not by any parsing under test.
try:
    import psycopg  # noqa: F401
except ImportError:
    stub = types.ModuleType("psycopg")
    stub.AsyncConnection = object
    sys.modules["psycopg"] = stub

from canon import (apple_sport, canonical, local_day, night_day, sleep_stage,
                   to_canonical_value)
from importers.apple_xml import _laps, _workout_fields, _elements
from parse import (duration_seconds, energy_kcal, dist_to_meters, parse_dt,
                   pool_length_from_text, pool_length_m)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "export.xml"

failures: list[str] = []


def check(label: str, got, want, tol: float | None = None) -> None:
    ok = abs(got - want) <= tol if tol is not None and got is not None else got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}: {got!r}")


# ------------------------------------------------------------------- units --
print("\nunit conversions")
check("kJ workout energy -> kcal", energy_kcal("1800", "kJ"), 430.2, tol=0.1)
check("kcal stays kcal", energy_kcal("500", "kcal"), 500.0, tol=0.01)
check("minutes -> seconds", duration_seconds("47.5", "min"), 2850.0, tol=0.01)
check("km -> metres", dist_to_meters("2", "km"), 2000.0, tol=0.01)
check("yards -> metres", dist_to_meters("100", "yd"), 91.44, tol=0.01)

# HAE labels a 50m pool as {"units":"m","qty":0.05}. No pool is under a metre.
check("HAE lapLength km-as-m bug", pool_length_m({"units": "m", "qty": 0.05}), 50.0)
check("honest 25m lapLength", pool_length_m({"units": "m", "qty": 25}), 25.0)
check("implausible lapLength rejected", pool_length_m({"units": "m", "qty": 900}), None)
check("xml lapLength '50 m'", pool_length_from_text("50 m"), 50.0)
check("xml lapLength '0.05 m'", pool_length_from_text("0.05 m"), 50.0)
check("xml lapLength junk", pool_length_from_text("unknown"), None)

print("\ncanonical values")
check("180 lb -> kg", to_canonical_value("weight", 180.0, "lb")[0], 81.6466, tol=0.001)
check("SpO2 0.97 -> 97%", to_canonical_value("blood_oxygen", 0.97, "%")[0], 97.0, tol=0.01)
check("SpO2 97 stays 97", to_canonical_value("blood_oxygen", 97.0, "%")[0], 97.0, tol=0.01)
check("3.2 km walked -> m", to_canonical_value("distance_walking", 3.2, "km")[0], 3200.0, tol=0.01)
check("HRV passes through", to_canonical_value("hrv_sdnn", 61.5, "ms")[0], 61.5, tol=0.01)
check("unknown metric", to_canonical_value("nonsense", 1.0, "x"), None)

# ----------------------------------------------------------------- aliases --
print("\nname mapping (never on user-visible strings)")
check("apple step count", canonical("apple", "HKQuantityTypeIdentifierStepCount"), "steps")
check("hae step count", canonical("hae", "step_count"), "steps")
check("both dialects agree on HRV",
      canonical("apple", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN")
      == canonical("hae", "heart_rate_variability"), True)
check("unmapped type is skipped, not guessed",
      canonical("apple", "HKCategoryTypeIdentifierToothbrushingEvent"), None)
# Category types carry a string in @value, so a numeric read yields NULL. They
# must stay unmapped until they get a parser of their own, or the import writes
# rows of nothing.
check("category types stay unmapped",
      canonical("apple", "HKCategoryTypeIdentifierAppleStandHour"), None)
check("newly mapped: HR recovery",
      canonical("apple", "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute"),
      "hr_recovery_1min")
check("newly mapped: water temperature",
      canonical("apple", "HKQuantityTypeIdentifierWaterTemperature"), "water_temperature")
check("walking speed km/hr -> m/s",
      to_canonical_value("walking_speed", 5.4, "km/hr")[0], 1.5, tol=0.001)
check("walking asymmetry fraction -> percent",
      to_canonical_value("walking_asymmetry", 0.04, "%")[0], 4.0, tol=0.001)
check("swimming activity", apple_sport("HKWorkoutActivityTypeSwimming"), "swim")
check("unknown activity falls back", apple_sport("HKWorkoutActivityTypeCurling"), "other")
check("sleep stage rem", sleep_stage("HKCategoryValueSleepAnalysisAsleepREM"), "rem")
check("sleep stage from HAE dialect", sleep_stage("deep"), "deep")

# -------------------------------------------------------------- local time --
print("\nlocal day attribution")
# Kyiv is UTC+3 in summer, so the day boundary UTC gets wrong is the early
# morning, not the late evening: 01:30 local is 22:30 UTC the *previous* day.
early = parse_dt("2026-08-21 01:30:00 +0300")
check("01:30 Kyiv session stays on its own day", local_day(early), early.date())
check("UTC grouping would have filed it a day early",
      early.astimezone(timezone.utc).date() != local_day(early), True)

late = parse_dt("2026-08-21 23:30:00 +0300")
check("late evening session also stays put", local_day(late), late.date())
check("sleep from 23:40 counts as next morning",
      night_day(parse_dt("2026-08-19 23:40:00 +0300")).isoformat(), "2026-08-20")
check("sleep from 01:00 counts as that morning",
      night_day(parse_dt("2026-08-20 01:00:00 +0300")).isoformat(), "2026-08-20")
check("an afternoon nap is not a night",
      night_day(parse_dt("2026-08-20 14:00:00 +0300")).isoformat(), "2026-08-20")

# ------------------------------------------------------------ xml workouts --
print("\nexport.xml workout extraction")
if not FIXTURE.exists():
    failures.append(f"fixture missing: {FIXTURE}")
    print(f"  FAIL  fixture missing: {FIXTURE}")
else:
    # _elements clears each element after yielding, so parse as we go rather
    # than collecting first — this mirrors how the importer consumes the stream,
    # and a test that collected elements would pass against a broken streamer.
    fields = []
    for elem in _elements(FIXTURE):
        if elem.tag == "Workout":
            f = _workout_fields(elem)
            f["laps"] = _laps(elem, "w", f["pool_length_m"])
            fields.append(f)

    check("two workouts found", len(fields), 2)
    swim = fields[0]
    check("sport", swim["sport"], "swim")
    check("indoor from swimming location type", swim["is_indoor"], True)
    check("duration", swim["duration_s"], 2850.0, tol=0.01)
    check("distance", swim["distance_m"], 2000.0, tol=0.01)
    check("energy converted from kJ", swim["active_kcal"], 430.2, tol=0.1)
    check("avg hr from WorkoutStatistics", swim["avg_hr"], 131.0, tol=0.01)
    check("max hr from WorkoutStatistics", swim["max_hr"], 162.0, tol=0.01)
    check("strokes from WorkoutStatistics", swim["stroke_count"], 1040.0, tol=0.01)
    check("pool length from metadata", swim["pool_length_m"], 50.0, tol=0.01)

    # The whole point of reading export.xml rather than HAE output.
    check("lap events survive streaming", len(swim["laps"]), 2)
    check("lap is numbered from 1", swim["laps"][0][1], 1)
    check("lap distance is one pool length", swim["laps"][0][4], 50.0, tol=0.01)
    check("pause events are not laps",
          all(r[1] in (1, 2) for r in swim["laps"]), True)

    walk = fields[1]
    check("walk sport", walk["sport"], "walk")
    # No totalDistance attribute on this one; it has to come from the statistic.
    check("distance falls back to WorkoutStatistics", walk["distance_m"], 2400.0, tol=0.01)
    check("no lap events on a walk", len(walk["laps"]), 0)

# ------------------------------------------------------------- gpx routes --
print("\ngpx route parsing")
from importers.routes import parse_gpx, haversine, _local

check("namespace stripped", _local("{http://www.topografix.com/GPX/1/1}trkpt"), "trkpt")
# ~111 km per degree of latitude; one thousandth is ~111 m. Kyiv longitude at
# 50°N is ~71 m per thousandth. Just check the order of magnitude and symmetry.
check("haversine ~111m per 0.001 lat",
      haversine((50.0, 30.0), (50.001, 30.0)), 111.0, tol=2.0)
check("haversine zero on identical point", haversine((50.0, 30.0), (50.0, 30.0)), 0.0, tol=1e-6)

RGPX = Path(__file__).resolve().parent / "fixtures" / "route.gpx"
pts = list(parse_gpx(RGPX))
check("three track points parsed", len(pts), 3)
ts, lat, lon, ele, speed = pts[0]
check("lat read", lat, 50.247561, tol=1e-6)
check("lon read", lon, 30.562085, tol=1e-6)
check("elevation read", ele, 120.5, tol=1e-6)
check("speed from extensions", speed, 0.80, tol=1e-6)
check("timestamp parsed", ts.year, 2026)
check("point without extensions still parses", pts[2][4], None)  # no speed

# HAE carries the track inline on the workout. Shape copied from a real payload
# (2026-10-21) — the ingest used to drop it, so post-backfill swims had no map.
from importers.routes import hae_points, route_summary

hae_route = [
    {"speed": 0.0688, "course": 139.8, "altitude": 448.77, "latitude": 13.701357,
     "longitude": -89.075888, "timestamp": "2026-10-17 08:05:34 -0500",
     "speedAccuracy": 0.93, "horizontalAccuracy": 2.83},
    {"latitude": 13.702357, "longitude": -89.075888,
     "timestamp": "2026-10-17 08:06:34 -0500"},
    {"latitude": None, "longitude": -89.0, "timestamp": "2026-10-17 08:07:34 -0500"},
]
hp = hae_points(hae_route)
check("HAE route: fix without latitude skipped", len(hp), 2)
check("HAE route: altitude -> ele", hp[0][3], 448.77, tol=1e-6)
check("HAE route: speed kept", hp[0][4], 0.0688, tol=1e-6)
check("HAE route: timestamp keeps its offset", hp[0][0].utcoffset().total_seconds(), -5 * 3600)
check("HAE route: missing altitude is None", hp[1][3], None)
check("HAE route: not a list -> no points", hae_points(None), [])
rs = route_summary("w1", "hae", hp)
check("route summary: point count", rs[2], 2)
check("route summary: ~111m over 0.001 lat", rs[5], 111.2, tol=1.0)
check("route summary: no points -> no row", route_summary("w1", "hae", []), None)

# ------------------------------------------------- HAE ingest parse path --
# Validated against a real Health Auto Export payload, so daily pushes populate
# the canonical layer instead of silently dropping metrics.
print("\nHAE metric coverage")
# Every metric name a real payload contained (minus sleep_analysis, handled
# separately, and apple_stand_hour, a category marker left unmapped on purpose).
HAE_METRICS = [
    "apple_sleeping_wrist_temperature", "running_speed", "swimming_distance",
    "walking_step_length", "vo2_max", "active_energy", "running_power",
    "weight_body_mass", "heart_rate_variability", "running_vertical_oscillation",
    "headphone_audio_exposure", "walking_asymmetry_percentage",
    "walking_double_support_percentage", "six_minute_walking_test_distance",
    "underwater_depth", "running_stride_length", "walking_running_distance",
    "basal_energy_burned", "environmental_audio_exposure", "cardio_recovery",
    "step_count", "blood_oxygen_saturation", "swimming_stroke_count",
    "flights_climbed", "heart_rate", "resting_heart_rate",
    "walking_heart_rate_average", "underwater_temperature", "physical_effort",
    "running_ground_contact_time", "time_in_daylight", "apple_stand_time",
    "apple_exercise_time", "respiratory_rate", "stair_speed_up",
    "stair_speed_down", "walking_speed",
]
unmapped = [m for m in HAE_METRICS if canonical("hae", m) is None]
check("every real HAE metric maps to canon", unmapped, [])
check("underwater_temperature -> water_temperature",
      canonical("hae", "underwater_temperature"), "water_temperature")
check("cardio_recovery -> hr_recovery_1min",
      canonical("hae", "cardio_recovery"), "hr_recovery_1min")
check("stair_speed_up -> stair_ascent_speed",
      canonical("hae", "stair_speed_up"), "stair_ascent_speed")

print("\nHAE sleep: stages present, no double-count")
from sleep import sleep_rows
# The real shape: staged values AND totalSleep AND asleep in one object.
night = {
    "rem": 2.0037, "core": 4.6227, "date": "2025-10-17 00:00:00 +0300",
    "deep": 0.6901, "awake": 0.0333, "inBed": 0.0, "asleep": 0.0,
    "source": "Vitalii Apple Watch",
    "sleepStart": "2025-10-16 22:33:05 +0300",
    "sleepEnd": "2025-10-17 05:54:04 +0300",
    "totalSleep": 7.3165,
}
rows = sleep_rows(night)
stages = {r[3]: (r[2] - r[1]).total_seconds() / 3600 for r in rows}
check("staged night has no duplicate 'asleep' segment",
      "asleep" in stages, False)
check("staged night keeps core/deep/rem (+awake here)",
      all(k in stages for k in ("core", "deep", "rem")), True)
check("staged total ~= totalSleep (not doubled)",
      stages["core"] + stages["deep"] + stages["rem"], 7.3165, tol=0.01)
check("night attributed to wake-up day", rows[0][5].isoformat(), "2025-10-17")

# An older-style night with only a total and no stages falls back to 'asleep'.
old_night = {"asleep": 0.0, "totalSleep": 6.5,
             "sleepStart": "2023-01-05 23:10:00 +0200", "source": "Watch"}
old_rows = sleep_rows(old_night)
old_stages = {r[3]: (r[2] - r[1]).total_seconds() / 3600 for r in old_rows}
check("stageless night falls back to one 'asleep' segment",
      list(old_stages), ["asleep"])
check("fallback uses totalSleep hours", old_stages.get("asleep"), 6.5, tol=0.01)

# ------------------------------------------------------------- HAE laps --
# Since 2026-09 HAE sends per-length `laps`. Shape copied from a real 25 m swim.
print("\nHAE lap splits")
from swim import derive_swim, hae_laps

lap_item = lambda i, s, e, dur: {
    "index": i, "start": f"2026-10-10 18:{s} -0300", "end": f"2026-10-10 18:{e} -0300",
    "activeDuration": dur, "elapsedDuration": dur, "boundaryKind": "interval",
    "metrics": {"distance": {"qty": 25, "units": "m", "origin": "sampleOverlapEstimate"}}}
hw = {"laps": [lap_item(1, "20:35", "20:53", 17.87), lap_item(2, "20:53", "21:17", 23.86),
               {"index": 3, "start": "2026-10-10 18:22:00 -0300"}]}   # no duration
hl = hae_laps(hw)
check("laps: item without duration skipped", len(hl), 2)
check("laps: activeDuration read", hl[0][2], 17.87, tol=1e-6)
check("laps: distance in metres", hl[1][3], 25.0)
check("laps: none -> empty", hae_laps({}), [])
d = derive_swim(50.0, 300.0, 25.0, 40.0, [], lap_count=len(hl),
                lap_total_s=sum(l[2] for l in hl))
check("laps feed the measured path", d["swolf_method"], "lap")
check("laps: moving time is the sum of lengths", d["moving_s"], 41.73, tol=1e-6)

# ---------------------------------------------------------- local time --
print("\nworkout timezones")
from datetime import timedelta
from localtime import PUSH_WINDOW, etc_zone, push_zone

check("UTC-3 is Etc/GMT+3 (POSIX sign)", etc_zone(timedelta(hours=-3)), "Etc/GMT+3")
check("UTC+3 is Etc/GMT-3", etc_zone(timedelta(hours=3)), "Etc/GMT-3")
check("UTC is Etc/UTC", etc_zone(timedelta(0)), "Etc/UTC")
check("half-hour offset has no Etc zone", etc_zone(timedelta(hours=5, minutes=30)), None)

end = parse_dt("2026-10-17 12:26:35 -0500")
check("timely push: its offset is trusted",
      push_zone("2026-10-17 06:52:06 -0600", end, end + timedelta(hours=2)), "Etc/GMT+6")
check("late push: phone may have moved, offset ignored",
      push_zone("2026-10-17 07:52:06 -0500", end, end + PUSH_WINDOW + timedelta(hours=1)), None)

check("device offset +2 -> Etc/GMT-2",
      __import__("localtime").device_zone("2026-11-02 07:10:17", "2026-11-02 05:10:17"),
      "Etc/GMT-2")

# ------------------------------------------------------------------ Garmin --
# Shapes copied from real Garmin Connect payloads and a real pool-swim FIT
# (2026-11-05). The FIT itself is not committed: it carries the user profile
# and device serials. These dicts keep the fields the parser reads.
print("\nGarmin")
from canon import CANON, garmin_sport
from importers.garmin import (Fit, active_lengths, daily_obs, fit_samples, sleep_rows,
                              workout_row)

check("typeKey lap_swimming -> pool swim", garmin_sport("lap_swimming"), ("swim", True))
check("typeKey open_water_swimming -> outdoor swim",
      garmin_sport("open_water_swimming"), ("swim", False))
check("unknown typeKey -> other", garmin_sport("underwater_basket_weaving"), ("other", None))

utc = timezone.utc
L = lambda s, dur, kind, stroke=None, n=None: {
    "start_time": datetime(2026, 11, 2, 5, 10, s, tzinfo=utc), "total_timer_time": dur,
    "total_elapsed_time": dur, "length_type": kind, "swim_stroke": stroke, "total_strokes": n}
fit = Fit(session={"pool_length": 50.0, "pool_length_unit": "metric", "total_strokes": 77,
                   "total_distance": 150.0, "total_elapsed_time": 400.0,
                   "avg_cadence": 22, "avg_heart_rate": 126},
          lengths=[L(0, 3.4, "idle"), L(4, 62.6, "active", "freestyle", 26),
                   L(7, 88.5, "active", "breaststroke", 25), L(9, 30.0, "idle"),
                   L(11, 60.0, "active", "freestyle", 26)],
          records=[{"timestamp": datetime(2026, 11, 2, 5, 10, s, tzinfo=utc), "heart_rate": hr}
                   for s, hr in ((1, 100), (2, 110), (3, 120))])
al = active_lengths(fit, 50.0)
check("idle lengths are rest, not swimming", len(al), 3)
check("active lengths numbered 1..n", [a[0] for a in al], [1, 2, 3])
check("stroke style kept per length", [a[4] for a in al],
      ["freestyle", "breaststroke", "freestyle"])
hr = [r for r in fit_samples(fit) if r[0] == "heartRateData"]
check("HR bucketed to one minute, averaged", (len(hr), hr[0][2]), (1, 110.0))

act = {"activityId": 1, "activityType": {"typeKey": "lap_swimming"},
       "startTimeGMT": "2026-11-02 05:10:17", "startTimeLocal": "2026-11-02 07:10:17",
       "elapsedDuration": 400.0, "distance": 150.0, "calories": 100.0, "bmrCalories": 20.0,
       "poolLength": 5000.0, "unitOfPoolLength": {"factor": 100.0}}
row = workout_row(act, fit, "Europe/Kyiv")
W = dict(zip(__import__("importers.garmin", fromlist=["WORKOUT_COLS"]).WORKOUT_COLS, row))
check("garmin workout id is namespaced", W["id"], "garmin:1")
check("pool length from FIT, metres", W["pool_length_m"], 50.0)
check("measured from lengths", W["swolf_method"], "lap")
check("moving time = active lengths only", W["moving_s"], 211.1, tol=1e-6)
check("duration stays elapsed (rest included)", W["duration_s"], 400.0)
check("active kcal = total - bmr", W["active_kcal"], 80.0)
W2 = dict(zip(W, workout_row(act, None, "Europe/Kyiv")))
check("summary poolLength is centimetres: 5000 -> 50 m", W2["pool_length_m"], 50.0)
check("no FIT yet: no invented SWOLF", (W2["swolf"], W2["swolf_method"]), (None, None))

obs = daily_obs("hrv", {"date": "2026-11-04"}, {"hrvSummary": {"lastNightAvg": 15}})
check("Garmin HRV is RMSSD, never SDNN", [o[1] for o in obs], ["hrv_rmssd"])
check("hrv_rmssd is a known canonical metric", "hrv_rmssd" in CANON, True)
obs = {o[1]: o[4] for o in daily_obs("stats", {"date": "2026-11-04"},
       {"totalSteps": 7692, "restingHeartRate": 66, "averageStressLevel": -1,
        "moderateIntensityMinutes": 2, "vigorousIntensityMinutes": 4})}
check("intensity minutes count vigorous double", obs.get("intensity_minutes"), 10.0)
check("Garmin's -1 means no data, not a value", "stress_avg" in obs, False)
check("daily summary lands on its calendar day",
      daily_obs("stats", {"date": "2026-11-04"}, {"totalSteps": 1}, "Europe/Kyiv")[0][3],
      datetime(2026, 11, 4).date())
w = daily_obs("body_comp", {}, {"dateWeightList": [
    {"date": 1793800000000, "weight": 103100.0, "bodyFat": None}]})
check("Garmin weight is grams", w[0][4], 103.1, tol=1e-6)

sl = sleep_rows({"dailySleepDTO": {"sleepStartTimestampGMT": 1793741873000,
                                   "sleepEndTimestampGMT": 1793768153000},
                 "sleepLevels": [
                     {"startGMT": "2026-11-03T21:37:53.0", "endGMT": "2026-11-03T21:41:53.0",
                      "activityLevel": 1.0},
                     {"startGMT": "2026-11-03T21:41:53.0", "endGMT": "2026-11-03T22:55:53.0",
                      "activityLevel": 0.0}]}, "Europe/Kyiv")
check("sleep levels: light -> core, 0 -> deep, plus in-bed",
      [r[3] for r in sl], ["inbed", "core", "deep"])
check("night filed on the wake-up day", {r[5] for r in sl}, {datetime(2026, 11, 4).date()})

# ------------------------------------------------------- approximate tracks --
print("\napproximate race tracks")
from importers.approx import (COLS as APPROX_COLS, destination, geometry, length_m,
                              load as load_approx, track as approx_track,
                              workout_row as approx_row)
from importers.routes import haversine

p = destination(8.0, 98.0, 90, 1000)
check("dead reckoning: 1 km east is 1 km away", haversine((8.0, 98.0), p), 1000.0, tol=0.5)
check("dead reckoning: due east keeps latitude", p[0], 8.0, tol=1e-4)
tri = {"start": [27.0, 33.9], "legs": [[90, 500], [215, 2500]], "close": True}
g = geometry(tri)
check("closed course returns to the start", g[-1], g[0])
loop = {"waypoints": [[0.0, 0.0], [0.0, 0.01], [0.01, 0.01], [0.0, 0.0]], "laps": 2}
check("two laps double the path, start point not repeated mid-way",
      length_m(geometry(loop)), 2 * length_m(geometry({**loop, "laps": 1})), tol=0.01)

races = load_approx()
check("every race in the data file has a time and a source",
      all(r.get("duration") and r.get("time_source") and r.get("result_ref") for r in races), True)
b19 = next(r for r in races if r["slug"] == "bosphorus-2019")
t = approx_track(b19)
check("track ends exactly at the official finish time",
      (t[-1][0] - t[0][0]).total_seconds(), 1 * 3600 + 19 * 60 + 26, tol=0.001)
check("track starts at the scheduled local hour", t[0][0].hour, 10)
R = dict(zip(APPROX_COLS, approx_row(b19, "Europe/Kyiv")))
check("approx row: official distance, not the drawn one", R["distance_m"], 6500.0)
check("approx row: venue zone set by hand", (R["tz_name"], R["tz_source"]),
      ("Europe/Istanbul", "manual"))
check("approx row: Kyiv calendar day for the dashboards",
      R["local_day"], datetime(2019, 7, 21).date())

# --------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
