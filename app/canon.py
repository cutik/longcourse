"""
Canonical vocabulary: raw provider names -> one metric name, one unit.

Why this lives in code rather than a table: it changes together with the
parsers, and a mapping that is not in git cannot be reviewed or rolled back.
`metric_meta` in Postgres is a materialised copy the app upserts at boot, so SQL
can join against it.

Three raw dialects have to land on the same names:

  hae     Health Auto Export JSON      "heart_rate_variability", units "ms"
  apple   native export.xml            "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
  samsung Samsung Health CSV archive   filled in when the archive arrives

Never map on user-visible strings. Workout and device names arrive in the
phone's language (the owner's is Ukrainian), so anything matching '%swim%'
silently returns nothing. Type identifiers and structural signals are stable;
display strings are not.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# The pipeline is single-user and lives in one place. Overridable via the add-on
# option `tz` (see config.yaml); this is the fallback.
DEFAULT_TZ = "Europe/Kyiv"

CUMULATIVE = "cumulative"        # summed over a day, from ONE source only
INSTANTANEOUS = "instantaneous"  # averaged over a day, all sources usable


# --------------------------------------------------------------- vocabulary --
# metric -> (kind, canonical unit, human label)
CANON: dict[str, tuple[str, str, str]] = {
    # heart & recovery
    "heart_rate":            (INSTANTANEOUS, "bpm",         "Heart rate"),
    "resting_heart_rate":    (INSTANTANEOUS, "bpm",         "Resting heart rate"),
    "walking_heart_rate":    (INSTANTANEOUS, "bpm",         "Walking heart rate"),
    "hrv_sdnn":              (INSTANTANEOUS, "ms",          "HRV (SDNN)"),
    "vo2max":                (INSTANTANEOUS, "ml/kg/min",   "VO2 max"),
    "respiratory_rate":      (INSTANTANEOUS, "bpm",         "Respiratory rate"),
    "blood_oxygen":          (INSTANTANEOUS, "%",           "Blood oxygen"),
    "wrist_temperature":     (INSTANTANEOUS, "degC",        "Sleeping wrist temperature"),
    "body_temperature":      (INSTANTANEOUS, "degC",        "Body temperature"),

    # body composition
    "weight":                (INSTANTANEOUS, "kg",          "Weight"),
    "body_fat":              (INSTANTANEOUS, "%",           "Body fat"),
    "lean_body_mass":        (INSTANTANEOUS, "kg",          "Lean body mass"),
    "bmi":                   (INSTANTANEOUS, "kg/m2",       "BMI"),
    "height":                (INSTANTANEOUS, "m",           "Height"),

    # daily activity — all cumulative, all double-count prone
    "steps":                 (CUMULATIVE,    "count",       "Steps"),
    "distance_walking":      (CUMULATIVE,    "m",           "Walking + running distance"),
    "flights_climbed":       (CUMULATIVE,    "count",       "Flights climbed"),
    "active_energy":         (CUMULATIVE,    "kcal",        "Active energy"),
    "basal_energy":          (CUMULATIVE,    "kcal",        "Basal energy"),
    "exercise_time":         (CUMULATIVE,    "min",         "Exercise minutes"),
    "stand_time":            (CUMULATIVE,    "min",         "Stand minutes"),

    # swim-specific quantity streams (also land in workout_samples per workout)
    "distance_swimming":     (CUMULATIVE,    "m",           "Swimming distance"),
    "swimming_strokes":      (CUMULATIVE,    "count",       "Swimming strokes"),

    # open water and diving — the archive turned out to carry these
    "water_temperature":     (INSTANTANEOUS, "degC",        "Water temperature"),
    "underwater_depth":      (INSTANTANEOUS, "m",           "Underwater depth"),

    # heart-rate recovery is a first-class recovery signal and there is already
    # a hr_recovery column on workouts for it
    "hr_recovery_1min":      (INSTANTANEOUS, "bpm",         "1-min heart-rate recovery"),

    # other sports
    "distance_cycling":      (CUMULATIVE,    "m",           "Cycling distance"),
    "running_speed":         (INSTANTANEOUS, "m/s",         "Running speed"),
    "running_power":         (INSTANTANEOUS, "W",           "Running power"),
    "running_stride_length": (INSTANTANEOUS, "m",           "Running stride length"),
    "running_vertical_osc":  (INSTANTANEOUS, "cm",          "Running vertical oscillation"),
    "running_ground_contact": (INSTANTANEOUS, "ms",         "Running ground contact time"),

    # effort and exposure
    "physical_effort":       (INSTANTANEOUS, "MET",         "Physical effort"),
    "time_in_daylight":      (CUMULATIVE,    "min",         "Time in daylight"),
    "audio_exposure_env":    (INSTANTANEOUS, "dBASPL",      "Environmental sound level"),
    "audio_exposure_phones": (INSTANTANEOUS, "dBASPL",      "Headphone audio level"),

    # gait / mobility
    "walking_speed":         (INSTANTANEOUS, "m/s",         "Walking speed"),
    "walking_step_length":   (INSTANTANEOUS, "cm",          "Walking step length"),
    "walking_asymmetry":     (INSTANTANEOUS, "%",           "Walking asymmetry"),
    "walking_double_support": (INSTANTANEOUS, "%",          "Double support time"),
    "walking_steadiness":    (INSTANTANEOUS, "%",           "Walking steadiness"),
    "stair_ascent_speed":    (INSTANTANEOUS, "m/s",         "Stair ascent speed"),
    "stair_descent_speed":   (INSTANTANEOUS, "m/s",         "Stair descent speed"),
    "six_minute_walk":       (INSTANTANEOUS, "m",           "Six-minute walk distance"),
}

# ------------------------------------------------------------------ aliases --
# (provider_dialect, raw_name) -> canonical metric.
# Raw names are lowercased before lookup, so entries here must be lowercase.
_APPLE = {
    "hkquantitytypeidentifierheartrate":                    "heart_rate",
    "hkquantitytypeidentifierrestingheartrate":             "resting_heart_rate",
    "hkquantitytypeidentifierwalkingheartrateaverage":      "walking_heart_rate",
    "hkquantitytypeidentifierheartratevariabilitysdnn":     "hrv_sdnn",
    "hkquantitytypeidentifiervo2max":                       "vo2max",
    "hkquantitytypeidentifierrespiratoryrate":              "respiratory_rate",
    "hkquantitytypeidentifieroxygensaturation":             "blood_oxygen",
    "hkquantitytypeidentifierapplesleepingwristtemperature": "wrist_temperature",
    "hkquantitytypeidentifierbodytemperature":              "body_temperature",
    "hkquantitytypeidentifierbodymass":                     "weight",
    "hkquantitytypeidentifierbodyfatpercentage":            "body_fat",
    "hkquantitytypeidentifierleanbodymass":                 "lean_body_mass",
    "hkquantitytypeidentifierbodymassindex":                "bmi",
    "hkquantitytypeidentifierheight":                       "height",
    "hkquantitytypeidentifierstepcount":                    "steps",
    "hkquantitytypeidentifierdistancewalkingrunning":       "distance_walking",
    "hkquantitytypeidentifierflightsclimbed":               "flights_climbed",
    "hkquantitytypeidentifieractiveenergyburned":           "active_energy",
    "hkquantitytypeidentifierbasalenergyburned":            "basal_energy",
    "hkquantitytypeidentifierappleexercisetime":            "exercise_time",
    "hkquantitytypeidentifierapplestandtime":               "stand_time",
    "hkquantitytypeidentifierdistanceswimming":             "distance_swimming",
    "hkquantitytypeidentifierswimmingstrokecount":          "swimming_strokes",
    "hkquantitytypeidentifierwatertemperature":             "water_temperature",
    "hkquantitytypeidentifierunderwaterdepth":              "underwater_depth",
    "hkquantitytypeidentifierheartraterecoveryoneminute":   "hr_recovery_1min",
    "hkquantitytypeidentifierdistancecycling":              "distance_cycling",
    "hkquantitytypeidentifierrunningspeed":                 "running_speed",
    "hkquantitytypeidentifierrunningpower":                 "running_power",
    "hkquantitytypeidentifierrunningstridelength":          "running_stride_length",
    "hkquantitytypeidentifierrunningverticaloscillation":   "running_vertical_osc",
    "hkquantitytypeidentifierrunninggroundcontacttime":     "running_ground_contact",
    "hkquantitytypeidentifierphysicaleffort":               "physical_effort",
    "hkquantitytypeidentifiertimeindaylight":               "time_in_daylight",
    "hkquantitytypeidentifierenvironmentalaudioexposure":   "audio_exposure_env",
    "hkquantitytypeidentifierheadphoneaudioexposure":       "audio_exposure_phones",
    "hkquantitytypeidentifierwalkingspeed":                 "walking_speed",
    "hkquantitytypeidentifierwalkingsteplength":            "walking_step_length",
    "hkquantitytypeidentifierwalkingasymmetrypercentage":   "walking_asymmetry",
    "hkquantitytypeidentifierwalkingdoublesupportpercentage": "walking_double_support",
    "hkquantitytypeidentifierapplewalkingsteadiness":       "walking_steadiness",
    "hkquantitytypeidentifierstairascentspeed":             "stair_ascent_speed",
    "hkquantitytypeidentifierstairdescentspeed":            "stair_descent_speed",
    "hkquantitytypeidentifiersixminutewalktestdistance":    "six_minute_walk",
}

_HAE = {
    "heart_rate":                        "heart_rate",
    "resting_heart_rate":                "resting_heart_rate",
    "walking_heart_rate_average":        "walking_heart_rate",
    "heart_rate_variability":            "hrv_sdnn",
    "vo2_max":                           "vo2max",
    "respiratory_rate":                  "respiratory_rate",
    "blood_oxygen_saturation":           "blood_oxygen",
    "apple_sleeping_wrist_temperature":  "wrist_temperature",
    "body_temperature":                  "body_temperature",
    "weight_body_mass":                  "weight",
    "body_fat_percentage":               "body_fat",
    "lean_body_mass":                    "lean_body_mass",
    "body_mass_index":                   "bmi",
    "height":                            "height",
    "step_count":                        "steps",
    "walking_running_distance":          "distance_walking",
    "flights_climbed":                   "flights_climbed",
    "active_energy":                     "active_energy",
    "basal_energy_burned":               "basal_energy",
    "apple_exercise_time":               "exercise_time",
    "apple_stand_time":                  "stand_time",
    "swimming_distance":                 "distance_swimming",
    "swimming_stroke_count":             "swimming_strokes",
    # Added after checking a real HAE payload — these names differ from the
    # export.xml identifiers, and without them the metrics never reach the
    # canonical layer or the dashboards. HAE's spellings, verified live:
    "underwater_temperature":            "water_temperature",
    "underwater_depth":                  "underwater_depth",
    "cardio_recovery":                   "hr_recovery_1min",
    "distance_cycling":                  "distance_cycling",
    "running_speed":                     "running_speed",
    "running_power":                     "running_power",
    "running_stride_length":             "running_stride_length",
    "running_vertical_oscillation":      "running_vertical_osc",
    "running_ground_contact_time":       "running_ground_contact",
    "physical_effort":                   "physical_effort",
    "time_in_daylight":                  "time_in_daylight",
    "environmental_audio_exposure":      "audio_exposure_env",
    "headphone_audio_exposure":          "audio_exposure_phones",
    "walking_speed":                     "walking_speed",
    "walking_step_length":               "walking_step_length",
    "walking_asymmetry_percentage":      "walking_asymmetry",
    "walking_double_support_percentage": "walking_double_support",
    "apple_walking_steadiness":          "walking_steadiness",
    "stair_speed_up":                    "stair_ascent_speed",
    "stair_speed_down":                  "stair_descent_speed",
    "six_minute_walking_test_distance":  "six_minute_walk",
}

ALIASES: dict[str, dict[str, str]] = {"apple": _APPLE, "hae": _HAE, "samsung": {}}


def canonical(dialect: str, raw_name: str) -> str | None:
    """Canonical metric name, or None if we do not model this metric.

    Returning None is normal and expected: a full Apple export carries well over
    a hundred record types (audio exposure, menstrual flow, headphone levels)
    that this project has no use for. They stay in the raw archive.
    """
    if not raw_name:
        return None
    return ALIASES.get(dialect, {}).get(raw_name.strip().lower())


# -------------------------------------------------------------- unit fixups --
# Multiplier onto the canonical unit, keyed by (canonical metric, raw unit).
_TO_CANON: dict[tuple[str, str], float] = {
    ("distance_walking", "km"): 1000.0,
    ("distance_walking", "mi"): 1609.344,
    ("distance_walking", "m"): 1.0,
    ("distance_swimming", "km"): 1000.0,
    ("distance_swimming", "yd"): 0.9144,
    ("distance_swimming", "m"): 1.0,
    ("active_energy", "kj"): 1 / 4.184,
    ("basal_energy", "kj"): 1 / 4.184,
    ("exercise_time", "s"): 1 / 60.0,
    ("exercise_time", "hr"): 60.0,
    ("stand_time", "s"): 1 / 60.0,
    ("stand_time", "hr"): 60.0,
    ("weight", "lb"): 0.45359237,
    ("weight", "g"): 0.001,
    ("lean_body_mass", "lb"): 0.45359237,
    ("height", "cm"): 0.01,
    ("height", "in"): 0.0254,
    ("body_fat", "fraction"): 100.0,
    ("blood_oxygen", "fraction"): 100.0,
    ("distance_cycling", "km"): 1000.0,
    ("distance_cycling", "mi"): 1609.344,
    ("distance_cycling", "m"): 1.0,
    ("walking_speed", "km/hr"): 1 / 3.6,
    ("walking_speed", "mi/hr"): 0.44704,
    ("running_speed", "km/hr"): 1 / 3.6,
    ("running_speed", "mi/hr"): 0.44704,
    ("stair_ascent_speed", "ft/s"): 0.3048,
    ("stair_descent_speed", "ft/s"): 0.3048,
    ("walking_step_length", "m"): 100.0,
    ("walking_step_length", "in"): 2.54,
    ("running_stride_length", "cm"): 0.01,
    ("running_vertical_osc", "m"): 100.0,
    ("time_in_daylight", "s"): 1 / 60.0,
    ("time_in_daylight", "hr"): 60.0,
    ("six_minute_walk", "km"): 1000.0,
    ("underwater_depth", "cm"): 0.01,
    # Fahrenheit is an offset conversion, not a multiplier — see the degf branch
    # in to_canonical_value(). It deliberately has no entry here.
}


def to_canonical_value(metric: str, value: float | None, raw_unit: str | None
                       ) -> tuple[float | None, str] | None:
    """Convert one reading into the canonical unit for its metric.

    Returns (value, canonical_unit), or None if the metric is unknown.

    Apple is inconsistent about units in ways that are easy to miss: workout
    energy is kJ despite field names saying kcal, percentages arrive both as
    0-100 and as 0-1, and HAE labels a 50m pool as 0.05 m. Anything not
    recognised is passed through unscaled rather than guessed at — a wrong
    scale factor is worse than a visibly odd number.
    """
    meta = CANON.get(metric)
    if meta is None:
        return None
    _, canon_unit, _ = meta
    if value is None:
        return None, canon_unit

    u = (raw_unit or "").strip().lower().replace("°", "deg")
    if u in ("degf", "f", "degreef"):
        if metric in ("wrist_temperature", "body_temperature"):
            return (value - 32.0) * 5.0 / 9.0, canon_unit

    # Percentages: Apple stores oxygen saturation and body fat as 0-1.
    if canon_unit == "%" and value is not None and 0 < value <= 1.0 and u in ("", "%", "fraction"):
        return value * 100.0, canon_unit

    factor = _TO_CANON.get((metric, u))
    if factor:
        return value * factor, canon_unit
    return value, canon_unit


# ------------------------------------------------------------------- sleep --
# export.xml category values, and the keys HAE uses in its sleep_analysis blobs.
SLEEP_STAGES = {
    "hkcategoryvaluesleepanalysisinbed":             "inbed",
    "hkcategoryvaluesleepanalysisasleepunspecified": "asleep",
    "hkcategoryvaluesleepanalysisasleep":            "asleep",
    "hkcategoryvaluesleepanalysisawake":             "awake",
    "hkcategoryvaluesleepanalysisasleepcore":        "core",
    "hkcategoryvaluesleepanalysisasleepdeep":        "deep",
    "hkcategoryvaluesleepanalysisasleeprem":         "rem",
    # HAE dialect. NOTE: HAE's per-night object carries both the staged values
    # (core/deep/rem) and a `totalSleep` sum at once. `totalSleep` is therefore
    # NOT a stage here — mapping it would write a duplicate 'asleep' segment on
    # top of the stages and double the night. sleep_rows() handles it explicitly
    # as a fallback only when no stages are present.
    "inbed":       "inbed",
    "asleep":      "asleep",
    "awake":       "awake",
    "core":        "core",
    "deep":        "deep",
    "rem":         "rem",
}

# Stages that represent actual sleep. 'asleep' is the unspecified bucket older
# watchOS wrote before it broke sleep into core/deep/rem — summing it together
# with the staged values would double-count those nights.
ASLEEP_STAGED = ("core", "deep", "rem")
ASLEEP_UNSPECIFIED = "asleep"


def sleep_stage(raw: str | None) -> str | None:
    if not raw:
        return None
    return SLEEP_STAGES.get(raw.strip().lower())


# ------------------------------------------------------------------ sports --
# HKWorkoutActivityType -> the sport vocabulary already used by `workouts`.
_APPLE_SPORTS = {
    "hkworkoutactivitytypeswimming":       "swim",
    "hkworkoutactivitytypewaterfitness":   "swim",
    "hkworkoutactivitytyperunning":        "run",
    "hkworkoutactivitytypewalking":        "walk",
    "hkworkoutactivitytypehiking":         "walk",
    "hkworkoutactivitytypecycling":        "bike",
    "hkworkoutactivitytypehandcycling":    "bike",
    "hkworkoutactivitytypeelliptical":     "cardio",
    "hkworkoutactivitytyperowing":         "cardio",
    "hkworkoutactivitytypestairclimbing":  "cardio",
    "hkworkoutactivitytypehighintensityintervaltraining": "cardio",
    "hkworkoutactivitytypefunctionalstrengthtraining":    "strength",
    "hkworkoutactivitytypetraditionalstrengthtraining":   "strength",
    "hkworkoutactivitytypecoretraining":   "strength",
    "hkworkoutactivitytypeyoga":           "mobility",
    "hkworkoutactivitytypeflexibility":    "mobility",
    "hkworkoutactivitytypecooldown":       "mobility",
    # The archive turned out to contain these; without entries they collapse
    # into "other" and become invisible in a per-sport breakdown.
    "hkworkoutactivitytypeunderwaterdiving": "dive",
    "hkworkoutactivitytypedownhillskiing":   "ski",
    "hkworkoutactivitytypefitnessgaming":    "cardio",
    "hkworkoutactivitytypehockey":           "other",
    "hkworkoutactivitytypecardiodance":      "cardio",
}


def apple_sport(activity_type: str | None) -> str:
    if not activity_type:
        return "other"
    return _APPLE_SPORTS.get(activity_type.strip().lower(), "other")


# --------------------------------------------------------------- local time --

def local_day(ts: datetime, tz: str = DEFAULT_TZ) -> date:
    """Calendar day in the athlete's timezone.

    Grouping on `ts::date` in UTC silently misfiles readings near the day
    boundary. For a UTC+N zone like Kyiv the damage is at the *early* end: a
    01:30 local session is 22:30 UTC the previous day, so it lands on yesterday
    and shows up as a phantom rest day plus a double session the day before.
    (Late evening is safe at UTC+3 — 23:00 local is only 20:00 UTC — but that
    stops being true for a negative offset, so the conversion is done properly
    rather than relying on the offset's sign.)
    """
    return ts.astimezone(ZoneInfo(tz)).date()


def night_day(ts: datetime, tz: str = DEFAULT_TZ) -> date:
    """The day a night's sleep is reported against — the day you wake up.

    Sleep beginning 23:40 Monday is Tuesday's night. The cut is made at 18:00
    local: anything starting after it belongs to the following day. Naps in the
    afternoon stay on their own day, which is the behaviour every sleep app has.
    """
    loc = ts.astimezone(ZoneInfo(tz))
    return (loc + timedelta(hours=6)).date() if loc.hour >= 18 else loc.date()
