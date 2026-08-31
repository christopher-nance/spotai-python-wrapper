"""Orchestration for the damage-claim collector.

Kept out of the client so the multi-step workflow - resolve, record, export,
link - can be read and changed on its own.

**Two things here are deliberate and easy to break.**

*Step order.* The Spot device is created **before** any footage exports are
submitted, so a failure part-way through leaves a recoverable record rather
than orphaned export jobs nothing points at.

*Anchor precision.* T0 comes from one of two places, and they are not equally
trustworthy. An LPR match is exact. A typed time is a person's estimate -
measured against LPR ground truth, off by a median of 7 minutes and as much as
15. A 90-second clip centred on an estimate will often miss the car, and a
clip of the **wrong car is worse than no clip** because it still looks like
evidence. So an estimated anchor produces a wide, scrubbable shared link
instead of narrow clips.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .claims import (
    Claim,
    ClaimCamera,
    ClaimResult,
    Clip,
    build_external_id,
    derive_status,
    device_name,
    plan_windows,
    select_share_cameras,
    signed_url_expiry,
    union_span,
)
from .errors import (
    ClaimExists,
    NoLprCamera,
    PlateNotFound,
    SpotAPIError,
    SpotError,
    SpotNotFoundError,
)
from .lpr import lookup_plate
from .matching import LIKELY, is_usable, rank_candidates
from .sitemap import SiteMap
from .timewin import get_zone, iso_z, parse_api_ts, parse_local

if TYPE_CHECKING:  # pragma: no cover
    from .client import SpotAI

CLAIM_TAG = "damage-claim"
MAX_EVENT_DURATION_MS = 600000
DEFAULT_BUFFER_MS = 30000

ANCHOR_PLATE = "plate"        # exact: the LPR saw this car at this instant
ANCHOR_ESTIMATE = "estimate"  # approximate: a person typed a time

# How far either side of an estimated time the shared link should span.
DEFAULT_ESTIMATE_WINDOW_MINUTES = 20


def today_at_site(site: SiteMap) -> str:
    """Today's date at the site, not on whatever machine is running this.

    A server in UTC would otherwise roll over to tomorrow's date at 7pm
    Chicago time, putting the wrong date into the claim's identity.
    """
    return datetime.now(timezone.utc).astimezone(
        get_zone(site.timezone)
    ).strftime("%Y-%m-%d")


class Anchor:
    """Where T0 came from, and how much to trust it."""

    def __init__(
        self,
        t0: datetime,
        kind: str,
        plate: str | None = None,
        confidence: float | None = None,
        date_text: str = "",
        candidates: list | None = None,
    ):
        self.t0 = t0
        self.kind = kind
        self.plate = plate
        self.confidence = confidence
        self.date_text = date_text
        self.candidates = candidates or []

    @property
    def precise(self) -> bool:
        return self.kind == ANCHOR_PLATE


def resolve_anchor(
    client: "SpotAI",
    site: SiteMap,
    plate: str | None,
    at: str | None,
    date: str | None,
    occurrence: str,
    min_confidence: float = LIKELY,
) -> Anchor:
    """Work out T0 and how much to trust it.

    Prefers an LPR match. Falls back to a typed time when the plate cannot be
    matched confidently and ``at`` was supplied - which is the normal case for
    the sites with no working LPR camera.
    """
    fallback_date = date or (at[:10] if at else None) or today_at_site(site)

    candidates: list = []
    if plate and site.lpr_camera_id and is_usable(plate):
        lookup = lookup_plate(
            client, site.lpr_camera_id, plate, fallback_date, site.timezone,
            fuzzy=False,
        )
        candidates = rank_candidates(plate, lookup.sightings)
        if candidates and candidates[0].score >= min_confidence:
            best = candidates[0]
            t0 = best.first_seen if occurrence == "first" else best.last_seen
            return Anchor(
                t0, ANCHOR_PLATE, best.plate, best.score, fallback_date, candidates
            )

    if at:
        t0 = parse_local(at, site.timezone)
        local_date = t0.astimezone(get_zone(site.timezone)).strftime("%Y-%m-%d")
        return Anchor(
            t0, ANCHOR_ESTIMATE, None, None, local_date, candidates
        )

    if not plate:
        raise ValueError("Supply plate= or at= (or both, plate preferred).")
    if not site.lpr_camera_id:
        raise NoLprCamera(
            site.location_name + " has no LPR camera configured, and no at= "
            "timestamp was supplied. Pass at=<site-local time>, or set "
            "lpr_camera_id on its SiteMap."
        )

    err = PlateNotFound(
        "No confident LPR match for " + repr(plate) + " on " + fallback_date
        + ". Supply at=<timestamp> as a fallback, lower min_confidence, or "
        "have someone pick from the candidates."
    )
    err.candidates = candidates  # type: ignore[attr-defined]
    raise err


def collect(
    client: "SpotAI",
    location: str | int,
    customer: str,
    plate: str | None = None,
    at: str | None = None,
    date: str | None = None,
    claim_ref: str | None = None,
    occurrence: str = "first",
    reuse_existing: bool = True,
    clips: str = "auto",
    min_confidence: float = LIKELY,
    estimate_window_minutes: int = DEFAULT_ESTIMATE_WINDOW_MINUTES,
) -> Claim:
    """Package a damage claim into a Spot case. Does not wait for exports.

    ``clips`` controls whether footage is exported:

    - ``"auto"`` (default) - narrow clips when the anchor is an LPR match;
      a wide scrubbable link when it is a typed estimate
    - ``"always"`` - export regardless
    - ``"never"`` - link only
    """
    if clips not in ("auto", "always", "never"):
        raise ValueError("clips must be 'auto', 'always' or 'never'")
    if not plate and not at:
        raise ValueError("Supply plate= or at= (or both, plate preferred).")

    site = client.site_map(location)
    anchor = resolve_anchor(
        client, site, plate, at, date, occurrence, min_confidence
    )
    external_id = build_external_id(
        site.slug, claim_ref, anchor.plate or plate, anchor.date_text
    )

    integration_id = client.ensure_integration()
    event_type_id = client.ensure_event_type()

    existing = find_device(client, integration_id, external_id, site)
    if existing:
        if not reuse_existing:
            raise ClaimExists("A claim already exists with id " + external_id)
        return claim_from_existing(client, existing, site, anchor, external_id)

    # 1. Record first. Identity before work, so nothing is orphaned.
    device = client.create_device(
        integration_id,
        name=device_name(customer, anchor.date_text),
        camera_ids=site.key_camera_ids,
        tags=[site.slug.title(), CLAIM_TAG],
        external_id=external_id,
    )
    device_id = int(device["id"])

    # 2. Export only when it is worth it.
    export = clips == "always" or (clips == "auto" and anchor.precise)
    cameras = plan_windows(site, anchor.t0)
    if export:
        for cam in cameras:
            try:
                job = client.create_footage_job(
                    cam.camera_id, cam.window_start, cam.window_end
                )
                cam.job_id = job.get("id") or job.get("redirectId")
                cam.state = "QUEUED"
            except SpotError as exc:
                cam.state = "FAILED"
                cam.error = str(exc)
    else:
        for cam in cameras:
            cam.state = "NOT_REQUESTED"

    share_link = make_share_link(
        client, cameras, anchor, site, estimate_window_minutes
    )

    # 3. Attach the event, carrying enough to rebuild the claim from Spot alone.
    try:
        client.create_event(
            integration_id,
            event_type_id=event_type_id,
            device_id=device_id,
            timestamp=iso_z(anchor.t0),
            duration_ms=min(site.transit_seconds * 1000, MAX_EVENT_DURATION_MS),
            attributes={
                "claim_ref": claim_ref or "",
                "plate": anchor.plate or (plate or ""),
                "typed_plate": plate or "",
                "customer": customer,
                "location": site.location_name,
                "share_link": share_link or "",
                "t0": iso_z(anchor.t0),
                "anchor": anchor.kind,
                "match_confidence": round(anchor.confidence or 0.0, 3),
                "footage_jobs": [
                    {
                        "camera_id": c.camera_id,
                        "job_id": c.job_id or 0,
                        "name": c.name,
                        "role": c.role,
                        "offset_seconds": c.offset_seconds,
                    }
                    for c in cameras
                ],
            },
        )
    except SpotError as exc:
        raise SpotAPIError(
            "Exports were submitted and device " + str(device_id) + " ("
            + external_id + ") was created, but attaching the event failed: "
            + str(exc) + "\nRe-run with the same arguments to retry; the "
            "existing device will be reused."
        ) from exc

    return Claim(
        id=external_id,
        t0=anchor.t0,
        device_id=device_id,
        event_id=latest_event_id(client, integration_id, device_id),
        location=site.location_name,
        status=derive_status([c.state for c in cameras]) if export else "link-only",
        share_link=share_link,
        cameras=cameras,
        anchor=anchor.kind,
        matched_plate=anchor.plate,
        match_confidence=anchor.confidence,
        candidates=[c.to_dict() for c in anchor.candidates],
    )


def fetch(
    client: "SpotAI", device_id: int, event_id: str | None = None
) -> ClaimResult:
    """Current state of a claim: status, clips, and anything that failed."""
    integration_id = client.ensure_integration()
    events = client.events(integration_id, device_ids=[device_id])
    if not events:
        raise SpotNotFoundError(
            "No events found for device " + str(device_id) + ". Spot ingests "
            "events asynchronously, so a claim created moments ago may not be "
            "queryable yet."
        )

    event = None
    if event_id:
        event = next((e for e in events if e.get("id") == event_id), None)
    if event is None:
        event = max(events, key=lambda e: e.get("created", ""))

    attrs = event.get("attributes") or {}
    jobs = attrs.get("footage_jobs") or []

    clips: list[Clip] = []
    problems: list[dict[str, Any]] = []
    states: list[str] = []

    for job in jobs:
        name = job.get("name", "")
        camera_id = int(job.get("camera_id", 0) or 0)
        job_id = int(job.get("job_id", 0) or 0)

        if not job_id:
            # Link-only claims never requested exports; that is not a failure.
            continue

        try:
            detail = client.get_footage_job(camera_id, job_id)
        except SpotError as exc:
            states.append("FAILED")
            problems.append({"camera": name, "state": "FAILED", "reason": str(exc)})
            continue

        state = detail.get("state", "UNKNOWN")
        states.append(state)

        if state == "SUCCEEDED" and detail.get("objectPath"):
            url = detail["objectPath"]
            clips.append(
                Clip(
                    camera_id=camera_id,
                    name=name,
                    role=job.get("role", ""),
                    offset_seconds=int(job.get("offset_seconds", 0) or 0),
                    url=url,
                    url_expires=signed_url_expiry(url),
                )
            )
        elif state != "SUCCEEDED":
            problems.append(
                {
                    "camera": name,
                    "state": state,
                    "reason": detail.get("stateDescription") or state,
                }
            )

    clips.sort(key=lambda c: c.offset_seconds)
    return ClaimResult(
        status="link-only" if not states else derive_status(states),
        share_link=attrs.get("share_link") or None,
        clips=clips,
        problems=problems,
        anchor=attrs.get("anchor") or ANCHOR_PLATE,
        matched_plate=attrs.get("plate") or None,
        match_confidence=attrs.get("match_confidence") or None,
    )


# ------------------------------------------------------------------ helpers
def make_share_link(
    client: "SpotAI",
    cameras: list[ClaimCamera],
    anchor: Anchor,
    site: SiteMap,
    estimate_window_minutes: int,
) -> str | None:
    """One public link covering every camera. Absence must not fail a claim.

    An estimated anchor gets a **wide** window - the reviewer scrubs to find
    the car rather than trusting a guessed instant.
    """
    try:
        if anchor.precise:
            start, end = union_span(cameras)
        else:
            pad = timedelta(minutes=max(1, estimate_window_minutes))
            start = iso_z(anchor.t0 - pad)
            end = iso_z(anchor.t0 + pad + timedelta(seconds=site.transit_seconds))
        shared = client.create_shared_search(
            select_share_cameras(cameras), start, end
        )
        return shared.get("link")
    except SpotError:
        return None


def find_device(
    client: "SpotAI", integration_id: int, external_id: str, site: SiteMap
) -> dict | None:
    """Look a claim up by external_id, narrowed by the site's tag.

    Spot's device list cannot filter on external_id, so narrow by tag and
    scan. Deterministic, and does not depend on the API's duplicate-key error
    shape - but it is O(devices at this site), which is the main cost of
    modelling one device per claim.
    """
    for device in client.devices(integration_id, tags=[site.slug.title()]):
        if device.get("external_id") == external_id:
            return device
    return None


def claim_from_existing(
    client: "SpotAI", device: dict, site: SiteMap, anchor: Anchor, external_id: str
) -> Claim:
    """Rebuild a handle for a claim that already exists (idempotent re-submit)."""
    device_id = int(device["id"])
    integration_id = client.ensure_integration()
    event_id = latest_event_id(client, integration_id, device_id)
    try:
        result = fetch(client, device_id, event_id)
        status, share_link = result.status, result.share_link
    except SpotError:
        status, share_link = "pending", None
    return Claim(
        id=external_id,
        t0=anchor.t0,
        device_id=device_id,
        event_id=event_id,
        location=site.location_name,
        status=status,
        share_link=share_link,
        reused=True,
        anchor=anchor.kind,
        matched_plate=anchor.plate,
        match_confidence=anchor.confidence,
    )


def latest_event_id(
    client: "SpotAI", integration_id: int, device_id: int
) -> str | None:
    """Most recent event on a device, or None.

    Spot accepts events with 202 and ingests them asynchronously, so this can
    legitimately return None immediately after creating one.
    """
    try:
        events = client.events(integration_id, device_ids=[device_id])
    except SpotError:
        return None
    if not events:
        return None
    return max(events, key=lambda e: e.get("created", "")).get("id")
