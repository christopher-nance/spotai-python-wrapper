"""Resolving a licence plate to T0 - the moment the car reached the entry.

Spot's LPR endpoint is a *report*, not an event stream: for a window it
returns per-plate aggregates (visits / first_seen / last_seen). A single pass
through the wash is therefore unambiguous, but a plate seen several times in
one window yields only its first and last sighting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import PlateNotFound
from .timewin import day_bounds_utc, iso_z, parse_api_ts

# Character pairs an ANPR engine most often confuses.
_CONFUSIONS = {
    "0": "O", "O": "0",
    "1": "I", "I": "1",
    "8": "B", "B": "8",
    "5": "S", "S": "5",
    "2": "Z", "Z": "2",
}

MAX_FUZZY_VARIANTS = 32

# Above this, first_seen is likely the approach rather than tunnel entry.
LONG_TRACK_SECONDS = 45


def normalize_plate(plate: str) -> str:
    """Upper-case and strip anything that is not alphanumeric."""
    return "".join(ch for ch in (plate or "").upper() if ch.isalnum())


def fuzzy_variants(plate: str, limit: int = MAX_FUZZY_VARIANTS) -> list[str]:
    """Plate spellings an OCR engine might have produced instead.

    Single-character substitutions only - enough to catch common misreads
    without exploding the query.
    """
    base = normalize_plate(plate)
    if not base:
        return []
    out = [base]
    for idx, ch in enumerate(base):
        swap = _CONFUSIONS.get(ch)
        if swap:
            candidate = base[:idx] + swap + base[idx + 1:]
            if candidate not in out:
                out.append(candidate)
        if len(out) >= limit:
            break
    return out[:limit]


@dataclass
class PlateSighting:
    plate: str
    visits: int
    first_seen: datetime
    last_seen: datetime

    @property
    def ambiguous(self) -> bool:
        """More than one pass through the wash in the queried window."""
        return self.visits > 1

    @property
    def track_seconds(self) -> int:
        """How long the LPR camera held this car in view.

        Typically 40-130 seconds: the camera acquires the car on approach and
        holds it through the queue, so first_seen is the approach and
        last_seen is nearer actual tunnel entry.
        """
        return int((self.last_seen - self.first_seen).total_seconds())

    @property
    def long_track(self) -> bool:
        return self.track_seconds >= LONG_TRACK_SECONDS


@dataclass
class PlateLookup:
    query: str
    sightings: list[PlateSighting] = field(default_factory=list)
    variants_tried: list[str] = field(default_factory=list)


def lookup_plate(
    client,
    camera_id: int,
    plate: str,
    date_text: str,
    tz_name: str,
    fuzzy: bool = False,
) -> PlateLookup:
    """Query the LPR report for one operating day."""
    start, end = day_bounds_utc(date_text, tz_name)
    variants = fuzzy_variants(plate) if fuzzy else [normalize_plate(plate)]

    report = client.lpr_report(camera_id, iso_z(start), iso_z(end), plates=variants)
    rows = (report or {}).get("plates") or []

    sightings: list[PlateSighting] = []
    for row in rows:
        try:
            sightings.append(
                PlateSighting(
                    plate=row.get("plate", "?"),
                    visits=int(row.get("visits", 0) or 0),
                    first_seen=parse_api_ts(row["first_seen"]),
                    last_seen=parse_api_ts(row["last_seen"]),
                )
            )
        except (KeyError, ValueError):
            # A malformed row should not sink the whole lookup.
            continue

    sightings.sort(key=lambda s: s.first_seen)
    return PlateLookup(query=plate, sightings=sightings, variants_tried=variants)


def resolve_t0(
    lookup: PlateLookup, occurrence: str = "first"
) -> tuple[datetime, PlateSighting]:
    """Pick T0 from a lookup result.

    ``occurrence`` chooses first_seen (approach) or last_seen (nearer actual
    tunnel entry).
    """
    if occurrence not in ("first", "last"):
        raise ValueError("occurrence must be 'first' or 'last'")
    if not lookup.sightings:
        raise PlateNotFound(
            "No LPR reads for plate " + repr(lookup.query) + " in that window.\n"
            "  Tried: " + ", ".join(lookup.variants_tried) + "\n"
            "  Try fuzzy=True, a different date, or pass at=<timestamp>."
        )
    # With several plates matched (fuzzy mode), prefer the busiest match.
    sighting = max(lookup.sightings, key=lambda s: s.visits)
    t0 = sighting.first_seen if occurrence == "first" else sighting.last_seen
    return t0, sighting
