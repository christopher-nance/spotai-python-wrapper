"""The damage-claim collector.

This is the one operation the Spot AI API does not provide: turning a plate
(or a timestamp) into a packaged, footage-linked case.
"""

from __future__ import annotations

import urllib.parse as _url
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .sitemap import SiteMap
from .timewin import iso_z, parse_api_ts, union_window, window_for

MAX_DEVICE_NAME = 40
MAX_EXTERNAL_ID = 255
MAX_SHARED_CAMERAS = 16

PENDING_STATES = {"QUEUED", "PROCESSING"}
FAILED_STATES = {"FAILED", "STALLED"}


# ----------------------------------------------------------------- naming
def device_name(customer: str, date_text: str, limit: int = MAX_DEVICE_NAME) -> str:
    """Build a device name that fits Spot's 40-character cap.

    Format is ``{customer} | {YYYY-MM-DD}``. The date is never dropped - it is
    what distinguishes a repeat customer's two claims - so a long name is
    truncated instead.
    """
    suffix = " | " + (date_text or "")
    room = limit - len(suffix)
    if room < 1:
        # Pathological date string; fall back to a hard truncation.
        return (customer or "Unknown")[:limit]
    name = (customer or "Unknown").strip() or "Unknown"
    if len(name) > room:
        name = name[:room].rstrip()
    return name + suffix


def device_name_part(
    customer: str,
    date_text: str,
    part: int,
    total: int,
    limit: int = MAX_DEVICE_NAME,
) -> str:
    """Name one device of a multi-device claim.

    Spot caps a device at 4 cameras, so a 23-camera claim needs 6 devices.
    They must be individually named but still read as one claim, and still fit
    in 40 characters - so the customer is squeezed, never the date or the part.
    """
    if total <= 1:
        return device_name(customer, date_text, limit)
    suffix = " | " + (date_text or "") + " (" + str(part) + "/" + str(total) + ")"
    room = limit - len(suffix)
    if room < 1:
        return (customer or "Unknown")[:limit]
    name = (customer or "Unknown").strip() or "Unknown"
    if len(name) > room:
        name = name[:room].rstrip()
    return name + suffix


def chunk_cameras(camera_ids: list[int], size: int = 4) -> list[list[int]]:
    """Split cameras into device-sized groups.

    Spot allows at most 4 cameras per integration device. Rather than silently
    dropping the rest, a claim that wants more gets more devices.
    """
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [camera_ids[i:i + size] for i in range(0, len(camera_ids), size)] or [[]]


def build_external_id(
    slug: str,
    claim_ref: str | None,
    plate: str | None,
    date_text: str,
    limit: int = MAX_EXTERNAL_ID,
) -> str:
    """Stable, unique, immutable key for a claim.

    Spot requires external_id to be unique within an integration, which is
    what makes ``collect_damage_claim`` idempotent against a double-submitted
    web form.
    """
    parts = [
        slug or "SITE",
        (claim_ref or "NOREF").strip() or "NOREF",
        (plate or "NOPLATE").strip().upper() or "NOPLATE",
        date_text or "NODATE",
    ]
    return ":".join(p.replace(":", "-") for p in parts)[:limit]


def part_external_id(base: str, part: int, limit: int = MAX_EXTERNAL_ID) -> str:
    """external_id for device N of a multi-device claim.

    Part 1 keeps the base id unchanged, so the idempotency lookup - which
    searches for the base - still finds the claim.
    """
    if part <= 1:
        return base
    suffix = "#P" + str(part)
    return (base[: limit - len(suffix)] + suffix)


# ----------------------------------------------------------------- status
def derive_status(states: list[str]) -> str:
    """Collapse per-camera export states into one claim status.

    ``partial`` is a normal outcome, not an edge case: Spot exports do
    occasionally wedge, and 15 usable clips beat a failed claim.
    """
    if not states:
        return "failed"
    if any(s in PENDING_STATES for s in states):
        return "pending"
    succeeded = sum(1 for s in states if s == "SUCCEEDED")
    if succeeded == len(states):
        return "ready"
    if succeeded == 0:
        return "failed"
    return "partial"


def _even_sample(items: list, k: int) -> list:
    """Take k items spread evenly across the list, keeping order."""
    if k <= 0:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def select_share_cameras(
    cameras: list["ClaimCamera"], limit: int = MAX_SHARED_CAMERAS
) -> list[int]:
    """Choose which cameras go in the shared link when there are too many.

    Spot caps a shared view at 16 cameras, but a site can easily exceed that -
    up to 15 on inspection arches plus 8 in the tunnel.

    Taking the first 16 in tunnel order would drop the *exit* arch, which is
    the footage that actually shows the damage. So the arches are kept first
    (exit, then entry), and any remaining slots go to an even spread of tunnel
    cameras rather than a contiguous run.

    Returns camera ids in tunnel order.
    """
    if len(cameras) <= limit:
        return [c.camera_id for c in cameras]

    exits = [c for c in cameras if c.role == "exit"]
    entries = [c for c in cameras if c.role == "entry"]
    tunnel = [c for c in cameras if c.role == "tunnel"]
    arches = exits + entries

    if len(arches) >= limit:
        # Arches alone overflow: sample both, favouring the exit arch.
        exit_k = min(len(exits), max(1, round(limit * len(exits) / len(arches))))
        chosen = _even_sample(exits, exit_k) + _even_sample(
            entries, limit - exit_k
        )
    else:
        chosen = arches + _even_sample(tunnel, limit - len(arches))

    keep = {c.camera_id for c in chosen}
    return [c.camera_id for c in cameras if c.camera_id in keep]


def signed_url_expiry(url: str) -> datetime | None:
    """When a Spot clip URL stops working.

    Spot hands back a pre-signed Google Cloud Storage URL re-signed on every
    request, valid one hour. Parsing the real deadline out of the query string
    means callers never have to guess.
    """
    try:
        q = dict(_url.parse_qsl(_url.urlparse(url).query))
        issued = datetime.strptime(q["X-Goog-Date"], "%Y%m%dT%H%M%SZ")
        return issued.replace(tzinfo=timezone.utc) + timedelta(
            seconds=int(q["X-Goog-Expires"])
        )
    except (KeyError, ValueError, TypeError):
        return None


# ------------------------------------------------------------------ models
@dataclass
class ClaimCamera:
    camera_id: int
    name: str
    role: str
    offset_seconds: int
    window_start: str
    window_end: str
    job_id: int | None = None
    state: str = "NOT_SUBMITTED"
    error: str | None = None


@dataclass
class Claim:
    """Handle returned immediately by ``collect_damage_claim``."""

    id: str
    t0: datetime
    device_id: int
    event_id: str | None
    location: str
    status: str = "pending"
    share_link: str | None = None
    reused: bool = False
    cameras: list[ClaimCamera] = field(default_factory=list)
    # Every device this claim spans. Spot caps a device at 4 cameras, so a
    # claim wanting more gets one device per four; device_id is the first.
    device_ids: list[int] = field(default_factory=list)
    # Where T0 came from: "plate" (exact LPR hit) or "estimate" (typed time).
    anchor: str = "plate"
    matched_plate: str | None = None
    match_confidence: float | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """True when a human should confirm the vehicle before this is used."""
        return self.anchor != "plate" or (self.match_confidence or 0) < 0.92

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "t0": iso_z(self.t0),
            "device_id": self.device_id,
            "device_ids": self.device_ids or [self.device_id],
            "event_id": self.event_id,
            "location": self.location,
            "status": self.status,
            "share_link": self.share_link,
            "reused": self.reused,
            "anchor": self.anchor,
            "matched_plate": self.matched_plate,
            "match_confidence": self.match_confidence,
            "candidates": self.candidates,
            "cameras": [vars(c) for c in self.cameras],
        }


@dataclass
class Clip:
    camera_id: int
    name: str
    role: str
    offset_seconds: int
    url: str
    url_expires: datetime | None


@dataclass
class ClaimResult:
    """Current state of a claim, returned by ``get_claim``."""

    status: str
    share_link: str | None
    clips: list[Clip] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    anchor: str = "plate"
    matched_plate: str | None = None
    match_confidence: float | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def link_only(self) -> bool:
        """No clips were exported - the anchor was an estimate."""
        return self.status == "link-only"


# ------------------------------------------------------------------- plan
def plan_windows(site: SiteMap, t0: datetime) -> list[ClaimCamera]:
    """Compute each camera's clip window from T0 and its offset.

    A wash takes minutes, so the exit cameras see the car long after the entry
    camera does. Every camera gets its own staggered window.
    """
    out: list[ClaimCamera] = []
    for cam in site.ordered_cameras():
        start, end = window_for(
            t0,
            cam.offset_seconds or 0,
            site.clip_seconds,
            site.pad_before_seconds,
            site.pad_after_seconds,
        )
        out.append(
            ClaimCamera(
                camera_id=cam.id,
                name=cam.name,
                role=cam.role,
                offset_seconds=cam.offset_seconds or 0,
                window_start=iso_z(start),
                window_end=iso_z(end),
            )
        )
    return out


def union_span(cameras: list[ClaimCamera]) -> tuple[str, str]:
    """Smallest window covering every camera, for the shared link."""
    start, end = union_window(
        [(parse_api_ts(c.window_start), parse_api_ts(c.window_end)) for c in cameras]
    )
    return iso_z(start), iso_z(end)
