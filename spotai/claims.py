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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "t0": iso_z(self.t0),
            "device_id": self.device_id,
            "event_id": self.event_id,
            "location": self.location,
            "status": self.status,
            "share_link": self.share_link,
            "reused": self.reused,
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

    @property
    def ready(self) -> bool:
        return self.status == "ready"


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
