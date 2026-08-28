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

# --------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")

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
