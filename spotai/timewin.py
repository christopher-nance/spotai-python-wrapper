"""Time-window math.

Pure functions only - no I/O, no API calls. This is where wrong-clip bugs
live, so it is the one module with real unit tests.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore


class TimeParseError(ValueError):
    """Raised when a user-supplied timestamp cannot be understood."""


_ACCEPTED = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def get_zone(tz_name: str):
    """Resolve an IANA timezone name.

    On Windows this needs the `tzdata` package; the error message says so
    rather than surfacing a bare KeyError.
    """
    if ZoneInfo is None:
        raise TimeParseError("Python 3.9+ with zoneinfo is required.")
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        raise TimeParseError(
            f"Unknown timezone {tz_name!r} ({exc}). On Windows, ensure the "
            "'tzdata' package is installed (it is in requirements.txt)."
        ) from exc


def parse_local(text: str, tz_name: str) -> datetime:
    """Parse a user-typed wall-clock timestamp into an aware UTC datetime.

    Accepts a trailing 'Z' or explicit UTC offset, in which case `tz_name` is
    ignored and the supplied offset wins.
    """
    raw = (text or "").strip()
    if not raw:
        raise TimeParseError("Empty timestamp.")

    # Explicit UTC / offset forms bypass the local timezone entirely.
    iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    if raw.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", raw):
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError as exc:
            raise TimeParseError(f"Could not parse {raw!r} as ISO 8601.") from exc
        return dt.astimezone(timezone.utc)

    for fmt in _ACCEPTED:
        try:
            naive = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        local = naive.replace(tzinfo=get_zone(tz_name))
        return local.astimezone(timezone.utc)

    raise TimeParseError(
        f"Could not parse {raw!r}. Try 'YYYY-MM-DD HH:MM' (24-hour), "
        "e.g. '2026-08-31 14:32'."
    )


def parse_api_ts(text: str) -> datetime:
    """Parse a timestamp returned by the Spot API into aware UTC.

    Spot returns ISO 8601; a missing timezone is treated as UTC.
    """
    raw = (text or "").strip()
    if not raw:
        raise TimeParseError("Empty API timestamp.")
    iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise TimeParseError(f"Unexpected API timestamp {raw!r}.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    """Format an aware datetime as the ISO 8601 Z form the API expects."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.astimezone(timezone.utc).microsecond // 1000:03d}Z"
    )


def to_local_str(dt: datetime, tz_name: str) -> str:
    """Render an aware datetime as readable site-local wall-clock time."""
    return dt.astimezone(get_zone(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")


def seed_offsets(n_tunnel: int, transit_seconds: int) -> list[int]:
    """Distribute tunnel cameras evenly between entry (0) and exit (transit).

    Camera i of n sits at transit * i / (n + 1), so no tunnel camera lands
    exactly on the entry or exit instant.
    """
    if n_tunnel <= 0:
        return []
    if transit_seconds < 0:
        raise ValueError("transit_seconds must be >= 0")
    return [
        round(transit_seconds * i / (n_tunnel + 1)) for i in range(1, n_tunnel + 1)
    ]


def window_for(
    t0: datetime,
    offset_seconds: int,
    clip_seconds: int,
    pad_before: int,
    pad_after: int,
) -> tuple[datetime, datetime]:
    """Compute one camera's clip window around T0.

    The camera is expected to see the car at `t0 + offset_seconds`; the window
    opens `pad_before` earlier and closes `pad_after` after the clip length.
    """
    if clip_seconds <= 0:
        raise ValueError("clip_seconds must be > 0")
    if pad_before < 0 or pad_after < 0:
        raise ValueError("padding must be >= 0")
    seen = t0 + timedelta(seconds=offset_seconds)
    return (
        seen - timedelta(seconds=pad_before),
        seen + timedelta(seconds=clip_seconds + pad_after),
    )


def union_window(
    windows: Iterable[Sequence[datetime]],
) -> tuple[datetime, datetime]:
    """Smallest window covering every supplied window.

    Used for the shared multi-camera link, which takes a single time range.
    """
    pairs = [(w[0], w[1]) for w in windows]
    if not pairs:
        raise ValueError("No windows to union.")
    return min(p[0] for p in pairs), max(p[1] for p in pairs)


def day_bounds_utc(date_text: str, tz_name: str) -> tuple[datetime, datetime]:
    """Local midnight-to-midnight for a YYYY-MM-DD date, as UTC bounds.

    Used to scope an LPR report query to one operating day.
    """
    start = parse_local(f"{date_text.strip()} 00:00:00", tz_name)
    return start, start + timedelta(days=1)
