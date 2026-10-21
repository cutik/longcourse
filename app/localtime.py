"""
Where in the world each workout happened, as a timezone.

Days stay cut in Kyiv time (`local_day`) so every chart shares one calendar.
This module adds what that discards: the wall-clock time at the place of the
workout, kept as `tz_name` plus `local_started_at`, for anything that one day
wants to plot "what time of day did I swim, where I was".

The obvious source is wrong. HAE writes every timestamp in the phone's timezone
*at export time*, not at the time of the event: the 2026-10-17 swim in
El Salvador arrived first as 06:52 -0600 and, after the phone moved on, as
07:52 -0500. So the offset in an HAE string is only evidence when the push came
soon after the workout.

Sources, best first. A better one always replaces a worse one, never the reverse:

  healthkit  HKTimeZone in export.xml workout metadata — Apple's own record
  gps        the zone at the first route fix (tzfpy, offline lookup)
  push       the offset of an HAE push that arrived within PUSH_WINDOW of the
             workout's end, as a fixed Etc/GMT zone
  nearby     copied from the nearest healthkit/gps workout within NEARBY_WINDOW
             — the walk to the pool usually has a track even when the swim
             does not
"""

from __future__ import annotations

from datetime import datetime, timedelta

from parse import parse_dt

RANK = {None: 0, "nearby": 1, "push": 2, "gps": 3, "healthkit": 4}
PUSH_WINDOW = timedelta(hours=6)
NEARBY_WINDOW = timedelta(hours=12)

try:
    from tzfpy import get_tz as _get_tz
except ImportError:          # tests and laptops without the wheel
    _get_tz = None


def etc_zone(offset: timedelta | None) -> str | None:
    """A fixed UTC offset as an IANA name. Etc/GMT signs are inverted by POSIX
    convention: UTC-3 is 'Etc/GMT+3'. Only whole hours exist as Etc zones."""
    if offset is None:
        return None
    secs = offset.total_seconds()
    if secs % 3600:
        return None
    h = int(secs // 3600)
    return "Etc/UTC" if h == 0 else f"Etc/GMT{'+' if h < 0 else '-'}{abs(h)}"


def zone_at(lat: float, lon: float) -> str | None:
    return _get_tz(lon, lat) if _get_tz else None


def push_zone(start: str | None, ended_at: datetime | None,
              received_at: datetime) -> str | None:
    """The zone an HAE start string implies, if the push was timely enough to trust."""
    ts = parse_dt(start)
    if ts is None or ended_at is None or received_at - ended_at > PUSH_WINDOW:
        return None
    return etc_zone(ts.utcoffset())


async def _apply(conn, valid: set[str], updates: list[tuple[str, str, str]]) -> int:
    """Write (workout_id, tz_name, source) where it outranks what is stored."""
    rows = [(tz, src, wid, src) for wid, tz, src in updates if tz in valid]
    if not rows:
        return 0
    async with conn.cursor() as c:
        await c.executemany(
            "UPDATE workouts SET tz_name = %s, tz_source = %s WHERE id = %s"
            " AND (CASE tz_source WHEN 'healthkit' THEN 4 WHEN 'gps' THEN 3"
            "      WHEN 'push' THEN 2 WHEN 'nearby' THEN 1 ELSE 0 END)"
            "   < (CASE %s WHEN 'healthkit' THEN 4 WHEN 'gps' THEN 3"
            "      WHEN 'push' THEN 2 WHEN 'nearby' THEN 1 ELSE 0 END)",
            rows)
    return len(rows)


async def resolve(conn, pushed: list[tuple[str, str | None]] | None = None,
                  received_at: datetime | None = None,
                  history: bool = False) -> dict:
    """Fill tz_name / tz_source / local_started_at.

    `pushed` is (workout_id, HAE start string) for the payload being ingested.
    `history=True` replays the push rule over every archived payload instead,
    which is what the derive job does.
    """
    cur = await conn.execute("SELECT name FROM pg_timezone_names")
    valid = {r[0] for r in await cur.fetchall()}
    stats = {}

    cur = await conn.execute(
        "SELECT id, raw->'metadata'->>'HKTimeZone' FROM workouts"
        " WHERE raw->'metadata' ? 'HKTimeZone' AND tz_source IS DISTINCT FROM 'healthkit'")
    stats["healthkit"] = await _apply(
        conn, valid, [(wid, tz, "healthkit") for wid, tz in await cur.fetchall()])

    if _get_tz:
        cur = await conn.execute(
            "SELECT w.id, rp.lat, rp.lon FROM workouts w"
            " JOIN route_points rp ON rp.workout_id = w.id AND rp.seq = 0"
            " WHERE w.tz_source IS NULL OR w.tz_source IN ('push', 'nearby')")
        stats["gps"] = await _apply(
            conn, valid, [(wid, zone_at(lat, lon), "gps")
                          for wid, lat, lon in await cur.fetchall()])

    if history:
        # Earliest push carrying each workout; the HAE id is external_id once a
        # row was matched onto an export.xml workout by start time.
        cur = await conn.execute(
            """
            SELECT DISTINCT ON (x.hid) wk.id, x.start, x.received_at, wk.ended_at
            FROM (SELECT w->>'id' AS hid, w->>'start' AS start, p.received_at
                  FROM raw_payloads p,
                       jsonb_array_elements(COALESCE(p.body->'data'->'workouts',
                                                     p.body->'workouts')) w) x
            JOIN workouts wk ON wk.external_id = x.hid AND wk.tz_source IS NULL
            ORDER BY x.hid, x.received_at
            """)
        candidates = await cur.fetchall()
    elif pushed:
        cur = await conn.execute(
            "SELECT id, ended_at FROM workouts WHERE id = ANY(%s) AND tz_source IS NULL",
            ([wid for wid, _ in pushed],))
        ends = dict(await cur.fetchall())
        now = received_at or datetime.now().astimezone()
        candidates = [(wid, start, now, ends[wid]) for wid, start in pushed if wid in ends]
    else:
        candidates = []
    stats["push"] = await _apply(
        conn, valid, [(wid, push_zone(start, end, at), "push")
                      for wid, start, at, end in candidates])

    cur = await conn.execute(
        """
        UPDATE workouts w SET tz_name = s.tz, tz_source = 'nearby'
        FROM (SELECT w2.id,
                     (SELECT k.tz_name FROM workouts k
                      WHERE k.tz_source IN ('healthkit', 'gps')
                        AND k.started_at BETWEEN w2.started_at - %(win)s
                                             AND w2.started_at + %(win)s
                      ORDER BY abs(extract(epoch FROM k.started_at - w2.started_at))
                      LIMIT 1) AS tz
              FROM workouts w2
              WHERE w2.tz_source IS NULL OR w2.tz_source = 'nearby') s
        WHERE w.id = s.id AND s.tz IS NOT NULL AND w.tz_name IS DISTINCT FROM s.tz
        """, {"win": NEARBY_WINDOW})
    stats["nearby"] = cur.rowcount or 0

    cur = await conn.execute(
        "UPDATE workouts SET local_started_at = started_at AT TIME ZONE tz_name"
        " WHERE tz_name IS NOT NULL"
        "   AND local_started_at IS DISTINCT FROM (started_at AT TIME ZONE tz_name)")
    stats["local_started_at"] = cur.rowcount or 0
    return stats
