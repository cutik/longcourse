"""
Value parsing shared by every ingest path.

Moved out of main.py so the CLI importers can use it without importing FastAPI.
Behaviour is unchanged — these are the same functions, and the unit repairs they
perform were expensive to discover. See CLAUDE.md, "Data reality".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Both Health Auto Export and the native export.xml use this shape:
#   2026-01-15 07:30:00 +0200
_HK_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?$")


def parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    m = _HK_DT.match(v.strip())
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


def as_float(v: Any) -> float | None:
    """Parse an XML attribute that should be a number, tolerating junk.

    export.xml attributes are all strings and a few are empty or non-numeric.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_DIST = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}


def to_meters(obj: Any) -> float | None:
    q, u = qty(obj), (units(obj) or "m").lower()
    return None if q is None else q * _DIST.get(u, 1.0)


def dist_to_meters(value: Any, unit: str | None) -> float | None:
    """Same conversion for the XML dialect, where value and unit are separate."""
    q = as_float(value)
    return None if q is None else q * _DIST.get((unit or "m").strip().lower(), 1.0)


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


def pool_length_from_text(text: str | None) -> float | None:
    """export.xml writes lap length as a MetadataEntry value like "50 m".

    Same sanity window as pool_length_m: a plausible pool is 5-100 m. Anything
    outside that is a misconfigured watch, not a pool, and must not silently
    become the divisor for every SWOLF in the session.
    """
    if not text:
        return None
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$", str(text))
    if not m:
        return None
    q = float(m.group(1))
    u = (m.group(2) or "m").lower()
    if u == "m" and q < 1:          # same km-mislabelled-as-m bug as HAE
        q *= 1000.0
    elif u and u != "m":
        q *= _DIST.get(u, 1.0)
    return q if 5 <= q <= 100 else None


def kcal(obj: Any) -> float | None:
    """HAE reports workout energy in kJ despite the field names."""
    q, u = qty(obj), (units(obj) or "").lower()
    if q is None:
        return None
    return q / 4.184 if u == "kj" else q


def energy_kcal(value: Any, unit: str | None) -> float | None:
    q = as_float(value)
    if q is None:
        return None
    return q / 4.184 if (unit or "").strip().lower() == "kj" else q


def duration_seconds(value: Any, unit: str | None) -> float | None:
    q = as_float(value)
    if q is None:
        return None
    u = (unit or "min").strip().lower()
    return q * {"s": 1.0, "sec": 1.0, "min": 60.0, "hr": 3600.0, "h": 3600.0}.get(u, 60.0)
