"""
Sleep segment parsing for the Health Auto Export dialect.

Split out of main.py so it can be tested without importing FastAPI. The native
export.xml sleep path is different (real per-record intervals) and lives in
importers/apple_xml.py; this handles HAE's one-object-per-night shape.
"""

from __future__ import annotations

from datetime import timedelta

from canon import night_day
from parse import parse_dt
from settings import TZ

PROVIDER = "apple"   # HAE data is Apple's; the provider column says so


def sleep_rows(point: dict) -> list[tuple]:
    """Turn one HAE sleep_analysis point into (provider, start, end, stage, source, day).

    HAE reports a night as one object with per-stage hour totals plus the
    night's boundaries. There are no real per-stage timestamps to recover, so
    each stage is written as a segment starting at sleepStart with its own
    duration: the totals are exact, the ordering within the night is not, and
    nothing downstream reads stage ordering.

    Verified against a real payload, one night carries the staged values
    (core/deep/rem) AND a `totalSleep` sum AND an `asleep` bucket at once.
    Writing all of them would double the night, so: emit the stages when
    present; fall back to the unspecified `asleep`/`totalSleep` bucket only when
    there are none (older watchOS). `awake` and `inBed` are always kept — they
    sit alongside sleep, they do not re-count it.
    """
    start = parse_dt(point.get("sleepStart") or point.get("inBedStart")
                     or point.get("date"))
    if not start:
        return []
    src = point.get("source") or ""
    day = night_day(start, TZ)
    rows: list[tuple] = []

    def hrs(*keys: str) -> float:
        for k in keys:
            v = point.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return 0.0

    def add(stage: str, hours: float) -> None:
        if hours > 0:
            rows.append((PROVIDER, start, start + timedelta(hours=hours),
                         stage, src, day))

    if hrs("core") + hrs("deep") + hrs("rem") > 0:
        add("core", hrs("core"))
        add("deep", hrs("deep"))
        add("rem", hrs("rem"))
    else:
        add("asleep", hrs("asleep", "totalSleep"))

    add("awake", hrs("awake"))
    add("inbed", hrs("inBed", "inbed"))
    return rows
