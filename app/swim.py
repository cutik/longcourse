"""
Sport classification and swim derivations.

Split out of main.py so the re-derive job can use it without importing FastAPI,
and so the estimator can be tested directly. The reasoning behind these numbers
is in CLAUDE.md under "Active time and why SWOLF is an estimate" — it is not
obvious from the code and was expensive to arrive at.
"""

from __future__ import annotations

from parse import parse_dt, qty

# Structural swim signals — present regardless of interface language.
SWIM_KEYS = ("swimDistance", "swimStroke", "swimCadence",
             "totalSwimmingStrokeCount", "lapLength")

# Time-series keys lifted into workout_samples.
SERIES_KEYS = ("swimStroke", "swimDistance", "heartRateData", "activeEnergy",
               "basalEnergy", "heartRateRecovery", "stepCount", "speed")


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


def moving_seconds(series: list[dict], total_distance_m: float | None) -> float | None:
    """Estimate active swimming time, excluding rest between sets.

    A binary "was this bucket active" filter does not work at per-minute
    resolution: half a minute at the wall still leaves 20+ metres in the
    bucket, so it counts as fully active. Measured on real sessions, a 5m
    threshold removed only 7% of elapsed time — i.e. nothing.

    So instead of finding rest, find the pace. The fastest buckets are the ones
    swum without pauses and represent true swimming speed; active time is then
    distance divided by that pace. Bucket boundaries stop mattering.

    Returns None when the series is too short to establish a pace, and the
    caller falls back to elapsed duration.

    The series must be at a consistent resolution across sources, or the same
    swim yields different answers depending on where it was imported from. The
    re-derive job buckets export.xml's finer records to one minute for exactly
    this reason.
    """
    pts = sorted(
        [(parse_dt(p.get("date")), qty(p)) for p in series if parse_dt(p.get("date"))],
        key=lambda x: x[0],
    )
    if len(pts) < 6 or not total_distance_m:
        return None
    gaps = [(pts[i + 1][0] - pts[i][0]).total_seconds() for i in range(len(pts) - 1)]
    width = sorted(gaps)[len(gaps) // 2]
    if width <= 0:
        return None

    dists = sorted((q or 0) for _, q in pts)
    top = dists[int(len(dists) * 0.75):]          # top quartile of buckets
    if not top:
        return None
    per_bucket = sum(top) / len(top)
    if per_bucket <= 0:
        return None

    speed = per_bucket / width                    # metres per second, swimming
    est = total_distance_m / speed
    # Never claim more active time than actually elapsed.
    return est


def derive_swim(distance_m: float | None, duration_s: float | None,
                pool_len: float | None, strokes: float | None,
                series: list[dict] | None = None,
                lap_count: int | None = None,
                lap_total_s: float | None = None) -> dict:
    """Compute the derived swim figures for one session.

    Two paths, and which one ran is recorded in `swolf_method` because they are
    not the same kind of number and must never be averaged together:

      'lap'       real per-length splits from HKWorkoutEventTypeLap. Lengths and
                  active time are counted, not inferred.
      'estimated' the top-quartile pace estimate over a per-minute series. A
                  session containing one all-out interval pulls the estimate
                  high and understates active time.

    swolf_gross is always computed from elapsed duration, so the cost of rest
    stays visible next to the rest-excluded figure.
    """
    out = {"lengths": None, "moving_s": None, "swolf": None,
           "swolf_gross": None, "pace_s_per_100m": None, "swolf_method": None,
           "pool_length_m": None}
    if not distance_m or distance_m <= 0:
        return out

    if lap_count and lap_count > 0 and lap_total_s and lap_total_s > 0:
        out["lengths"] = lap_count
        out["moving_s"] = lap_total_s
        out["swolf_method"] = "lap"
        # Recover pool length from the splits when the workout metadata did not
        # carry it: with one lap per length, distance / laps is the length. This
        # only feeds the returned pool_length_m hint; it is validated to a
        # plausible 5-100 m so a mis-shaped session cannot invent a pool.
        if not pool_len:
            cand = round(distance_m / lap_count)
            if 5 <= cand <= 100:
                out["pool_length_m"] = float(cand)
                pool_len = float(cand)
    elif pool_len:
        out["lengths"] = max(1, round(distance_m / pool_len))
        est = moving_seconds(series or [], distance_m)
        if est is None or (duration_s and est > duration_s):
            est = duration_s
        out["moving_s"] = est
        out["swolf_method"] = "estimated" if est is not None else None
    else:
        # Without a pool length there is no "length" to count strokes against,
        # so SWOLF is undefined. Pace still is not — but it needs active time,
        # which without a series is just elapsed time including rest, and that
        # is a materially different number. Left NULL rather than guessed.
        return out

    lengths, moving_s = out["lengths"], out["moving_s"]
    if strokes and lengths:
        spl = strokes / lengths
        if moving_s:
            out["swolf"] = moving_s / lengths + spl
        if duration_s:
            out["swolf_gross"] = duration_s / lengths + spl
    if moving_s:
        out["pace_s_per_100m"] = moving_s / (distance_m / 100.0)
    if out["swolf"] is None:
        out["swolf_method"] = None
    return out
