"""
Generate the Grafana dashboard JSON in grafana/dashboards/.

    python3 grafana/build_dashboards.py

The JSON is committed so it can be imported without running anything, but it is
generated so the panels stay consistent and a change to, say, the pool-length
filter does not have to be repeated by hand across a dozen hand-edited blobs.

Every dashboard declares a `ds` datasource variable rather than hardcoding a
UID. The target is the Grafana that already ships with TeslaMate, where the
longcourse datasource UID is whatever that instance assigned it — importing
should not require editing JSON.

Read-only by construction: the datasource should point at the longcourse_ro
role, so a dashboard cannot write.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "grafana-postgresql-datasource", "uid": "${ds}"}


def target(sql: str) -> dict:
    return {"format": "time_series", "rawQuery": True, "rawSql": sql,
            "refId": "A", "datasource": DS, "editorMode": "code"}


def panel(kind: str, title: str, sql: str, x: int, y: int, w: int, h: int,
          unit: str | None = None, desc: str = "", extra: dict | None = None,
          fmt: str = "time_series") -> dict:
    t = target(sql)
    t["format"] = fmt
    p = {
        "type": kind, "title": title, "description": desc,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS, "targets": [t],
        "fieldConfig": {"defaults": {"custom": {}}, "overrides": []},
        "options": {},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if extra:
        for k, v in extra.items():
            if k in ("fieldConfig", "options"):
                p[k] = {**p[k], **v}
            else:
                p[k] = v
    return p


def bars(**kw) -> dict:
    kw.setdefault("extra", {})
    kw["extra"] = {**kw["extra"], "fieldConfig": {
        "defaults": {"custom": {"drawStyle": "bars", "fillOpacity": 70,
                                "lineWidth": 0}, **({"unit": kw["unit"]} if kw.get("unit") else {})},
        "overrides": []}}
    return panel("timeseries", **kw)


def dashboard(uid: str, title: str, panels: list[dict],
              variables: list[dict] | None = None, from_: str = "now-1y") -> dict:
    tmpl = [{
        "name": "ds", "label": "Datasource", "type": "datasource",
        "query": "grafana-postgresql-datasource", "current": {}, "hide": 0,
    }] + (variables or [])
    return {
        "uid": uid, "title": title, "tags": ["longcourse"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "refresh": "30m", "editable": True,
        "time": {"from": from_, "to": "now"},
        "templating": {"list": tmpl},
        "panels": panels,
    }


def query_var(name: str, label: str, sql: str, current: str | None = None,
              multi: bool = False, include_all: bool = False) -> dict:
    v = {"name": name, "label": label, "type": "query", "datasource": DS,
         "query": sql, "refresh": 1, "multi": multi, "includeAll": include_all,
         "hide": 0, "sort": 1}
    if current:
        v["current"] = {"text": current, "value": current, "selected": True}
    return v


# ------------------------------------------------------------------- swim ---
# pool_length_m defaults to 50 on purpose. SWOLF sums seconds and strokes per
# length, so the single genuine 25m session in the history is not comparable to
# the rest and must not be allowed to sit inside a trend line.
swim = dashboard(
    "longcourse-swim", "Longcourse · Swim",
    variables=[
        query_var("pool", "Pool length (m)",
                  "SELECT DISTINCT pool_length_m::text FROM v_swim_sessions "
                  "WHERE pool_length_m IS NOT NULL ORDER BY 1", current="50"),
        query_var("method", "SWOLF method",
                  "SELECT DISTINCT swolf_method FROM v_swim_sessions "
                  "WHERE swolf_method IS NOT NULL ORDER BY 1",
                  multi=True, include_all=True),
    ],
    panels=[
        panel("stat", "Sessions", """
SELECT COUNT(*) AS value FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
""", 0, 0, 4, 4, fmt="table",
              desc="Pool sessions at the selected course length."),
        panel("stat", "Total distance", """
SELECT SUM(distance_m) AS value FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
""", 4, 0, 4, 4, unit="lengthm", fmt="table"),
        panel("stat", "Median pace", """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY pace_s_per_100m) AS value
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
""", 8, 0, 4, 4, unit="s", fmt="table",
              desc="Seconds per 100 m over active (rest-excluded) time."),
        panel("stat", "Median SWOLF", """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY swolf) AS value
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
  AND swolf_method IN ($method)
""", 12, 0, 4, 4, fmt="table",
              desc="Seconds per length plus strokes per length. Only comparable "
                   "within one pool length and one swolf_method."),
        panel("stat", "Median active ratio", """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY active_ratio) AS value
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
""", 16, 0, 4, 4, unit="percentunit", fmt="table",
              desc="Active time over elapsed time. The remainder was spent at the wall."),
        panel("stat", "Real lap splits", """
SELECT COUNT(*) AS value FROM v_swim_sessions
WHERE $__timeFilter(time) AND swolf_method = 'lap'
""", 20, 0, 4, 4, fmt="table",
              desc="Sessions where SWOLF was measured from per-length splits "
                   "rather than estimated. Zero means every figure here is an estimate."),

        bars(title="Distance per session", sql="""
SELECT time, distance_m AS "distance"
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
ORDER BY 1
""", x=0, y=4, w=12, h=8, unit="lengthm"),

        panel("timeseries", "Pace (s / 100 m, lower is faster)", """
SELECT time, pace_s_per_100m AS "pace"
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
ORDER BY 1
""", 12, 4, 12, 8, unit="s",
              extra={"fieldConfig": {"defaults": {
                  "unit": "s", "custom": {"drawStyle": "line", "showPoints": "always",
                                          "lineWidth": 2, "spanNulls": True}},
                  "overrides": []}}),

        panel("timeseries", "SWOLF: rest-excluded vs gross", """
SELECT time, swolf AS "swolf (active)", swolf_gross AS "swolf (incl. rest)"
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
  AND swolf_method IN ($method)
ORDER BY 1
""", 0, 12, 12, 8,
              desc="The gap between the two is time spent standing at the wall. "
                   "Only the lower line is a technique signal.",
              extra={"fieldConfig": {"defaults": {"custom": {
                  "drawStyle": "line", "showPoints": "always", "lineWidth": 2,
                  "spanNulls": True}}, "overrides": []}}),

        panel("timeseries", "Strokes per length", """
SELECT time, strokes_per_length AS "strokes/length"
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
ORDER BY 1
""", 12, 12, 12, 8,
              desc="Apple Watch undercounts strokes — roughly 22-27 per 50 m against "
                   "30-40 counted by hand. The bias looks consistent, so the trend is "
                   "usable but the absolute value is not.",
              extra={"fieldConfig": {"defaults": {"custom": {
                  "drawStyle": "line", "showPoints": "always", "lineWidth": 2,
                  "spanNulls": True}}, "overrides": []}}),

        panel("table", "Sessions", """
SELECT time AS "date", distance_m AS "m", ROUND(duration_s/60) AS "elapsed min",
       ROUND(moving_s/60) AS "active min", ROUND(active_ratio::numeric, 2) AS "ratio",
       lengths, ROUND(strokes_per_length::numeric, 1) AS "str/len",
       ROUND(swolf::numeric, 1) AS swolf, swolf_method AS "method",
       ROUND(pace_s_per_100m::numeric) AS "s/100m", avg_hr, max_hr
FROM v_swim_sessions
WHERE $__timeFilter(time) AND pool_length_m = $pool::float8
ORDER BY time DESC
""", 0, 20, 24, 10, fmt="table"),
    ])

# --------------------------------------------------------- training load ---
load = dashboard(
    "longcourse-load", "Longcourse · Training load",
    variables=[query_var("sport", "Sport",
                         "SELECT DISTINCT sport FROM v_training_load ORDER BY 1",
                         multi=True, include_all=True)],
    panels=[
        bars(title="Weekly distance by sport", sql="""
SELECT time, sport, SUM(distance_m) AS distance
FROM v_training_load
WHERE $__timeFilter(time) AND sport IN ($sport)
GROUP BY 1, 2 ORDER BY 1
""", x=0, y=0, w=12, h=9, unit="lengthm",
             extra={"options": {"legend": {"displayMode": "list", "placement": "bottom"}}}),

        bars(title="Weekly active hours by sport", sql="""
SELECT time, sport, SUM(active_s)/3600.0 AS hours
FROM v_training_load
WHERE $__timeFilter(time) AND sport IN ($sport)
GROUP BY 1, 2 ORDER BY 1
""", x=12, y=0, w=12, h=9, unit="h",
             desc="Active time where it is known, elapsed time otherwise."),

        bars(title="Sessions per week", sql="""
SELECT time, sport, SUM(sessions) AS sessions
FROM v_training_load
WHERE $__timeFilter(time) AND sport IN ($sport)
GROUP BY 1, 2 ORDER BY 1
""", x=0, y=9, w=12, h=8),

        panel("timeseries", "Average heart rate per week", """
SELECT time, sport, AVG(avg_hr) AS hr
FROM v_training_load
WHERE $__timeFilter(time) AND sport IN ($sport)
GROUP BY 1, 2 ORDER BY 1
""", 12, 9, 12, 8, unit="none"),

        panel("table", "Recent sessions, all sports", """
SELECT time AS "start", sport, is_indoor AS "indoor", distance_m AS "m",
       ROUND(duration_s/60) AS "min", ROUND(avg_kmh::numeric, 2) AS "km/h",
       ROUND(active_kcal::numeric) AS kcal, avg_hr, max_hr
FROM v_sessions
WHERE $__timeFilter(time) AND sport IN ($sport)
ORDER BY time DESC LIMIT 200
""", 0, 17, 24, 10, fmt="table"),
    ])

# ------------------------------------------------------ sleep & recovery ---
sleep = dashboard(
    "longcourse-recovery", "Longcourse · Sleep & recovery",
    panels=[
        bars(title="Sleep by stage", sql="""
SELECT time, deep_s/3600.0 AS deep, rem_s/3600.0 AS rem, core_s/3600.0 AS core,
       awake_s/3600.0 AS awake
FROM v_sleep_nights
WHERE $__timeFilter(time)
ORDER BY 1
""", x=0, y=0, w=16, h=9, unit="h",
             desc="Nights are filed against the morning you woke up on. Nights that "
                  "predate staged sleep tracking show as a single unspecified block "
                  "in the asleep total and contribute nothing to these stages.",
             extra={"fieldConfig": {"defaults": {
                 "unit": "h", "custom": {"drawStyle": "bars", "fillOpacity": 80,
                                         "lineWidth": 0, "stacking": {"mode": "normal"}}},
                 "overrides": []}}),

        panel("timeseries", "Sleep efficiency", """
SELECT time, efficiency FROM v_sleep_nights
WHERE $__timeFilter(time) AND efficiency IS NOT NULL ORDER BY 1
""", 16, 0, 8, 9, unit="percentunit",
              desc="Asleep over time in bed. Blank when no in-bed record exists."),

        panel("timeseries", "HRV (SDNN)", """
SELECT time, value AS hrv, min, max FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'hrv_sdnn' ORDER BY 1
""", 0, 9, 8, 8, unit="ms"),

        panel("timeseries", "Resting heart rate", """
SELECT time, value AS rhr FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'resting_heart_rate' ORDER BY 1
""", 8, 9, 8, 8),

        panel("timeseries", "Respiratory rate", """
SELECT time, value AS "breaths/min" FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'respiratory_rate' ORDER BY 1
""", 16, 9, 8, 8),

        panel("timeseries", "VO2 max", """
SELECT time, value AS vo2max FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'vo2max' ORDER BY 1
""", 0, 17, 8, 8),

        panel("timeseries", "Blood oxygen", """
SELECT time, value AS spo2, min, max FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'blood_oxygen' ORDER BY 1
""", 8, 17, 8, 8, unit="percent"),

        panel("timeseries", "Sleeping wrist temperature", """
SELECT time, value AS "deviation" FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'wrist_temperature' ORDER BY 1
""", 16, 17, 8, 8, unit="celsius"),
    ])

# --------------------------------------------------------- body/activity ---
body = dashboard(
    "longcourse-body", "Longcourse · Body & activity",
    panels=[
        panel("timeseries", "Weight", """
SELECT time, value AS kg FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'weight' ORDER BY 1
""", 0, 0, 12, 9, unit="kg",
              extra={"fieldConfig": {"defaults": {"unit": "kg", "custom": {
                  "drawStyle": "line", "showPoints": "always", "spanNulls": True,
                  "lineWidth": 2}}, "overrides": []}}),

        panel("timeseries", "Body fat", """
SELECT time, value AS pct FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'body_fat' ORDER BY 1
""", 12, 0, 12, 9, unit="percent"),

        bars(title="Steps", sql="""
SELECT time, value AS steps FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'steps' ORDER BY 1
""", x=0, y=9, w=12, h=8,
             desc="One source per day only. iPhone and Watch both record steps, so "
                  "summing every record would roughly double the real figure; the "
                  "best-covered source wins."),

        bars(title="Active energy", sql="""
SELECT time, value AS kcal FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'active_energy' ORDER BY 1
""", x=12, y=9, w=12, h=8, unit="kcal"),

        bars(title="Exercise minutes", sql="""
SELECT time, value AS minutes FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'exercise_time' ORDER BY 1
""", x=0, y=17, w=12, h=8, unit="m"),

        bars(title="Walking + running distance", sql="""
SELECT time, value AS metres FROM v_daily_metrics
WHERE $__timeFilter(time) AND metric = 'distance_walking' ORDER BY 1
""", x=12, y=17, w=12, h=8, unit="lengthm"),

        panel("table", "Which source is being trusted for each metric", """
SELECT metric, kind, unit, source, MAX(samples) AS "samples/day", COUNT(*) AS days
FROM v_daily_metrics
WHERE $__timeFilter(time)
GROUP BY 1,2,3,4 ORDER BY 1,4
""", 0, 25, 24, 9, fmt="table",
              desc="Sanity check for the double-counting rule. A cumulative metric "
                   "showing two different sources across the range means the primary "
                   "device changed — expect a step in the series there."),
    ])

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, d in [("swim", swim), ("training-load", load),
                    ("sleep-recovery", sleep), ("body-activity", body)]:
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        print(f"{path}  ({len(d['panels'])} panels)")
