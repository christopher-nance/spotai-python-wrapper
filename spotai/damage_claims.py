"""Orchestration for the damage-claim collector.

Kept out of the client so the multi-step workflow - resolve, record, export,
link - can be read and changed on its own.

**Step order matters.** The Spot device is created *before* any footage
exports are submitted, so a failure part-way through leaves a recoverable
record rather than a set of orphaned export jobs nothing points at.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    signed_url_expiry,
    union_span,
)
from .errors import ClaimExists, NoLprCamera, SpotAPIError, SpotError, SpotNotFoundError
from .lpr import lookup_plate, resolve_t0
from .sitemap import SiteMap
from .timewin import get_zone, iso_z, parse_local

if TYPE_CHECKING:  # pragma: no cover
    from .client import SpotAI

CLAIM_TAG = "damage-claim"
MAX_EVENT_DURATION_MS = 600000
DEFAULT_BUFFER_MS = 30000


def today_at_site(site: SiteMap) -> str:
    """Today's date at the site, not on whatever machine is running this.

    A server in UTC would otherwise roll over to tomorrow's date at 7pm
    Chicago time, putting the wrong date into the claim's identity.
    """
    return datetime.now(timezone.utc).astimezone(get_zone(site.timezone)).strftime(
        "%Y-%m-%d"
    )


def resolve_anchor(
    client: "SpotAI",
    site: SiteMap,
    plate: str | None,
    at: str | None,
    date: str | None,
    occurrence: str,
    fuzzy: bool,
) -> tuple[datetime, str | None, str]:
    """Work out T0, the plate actually matched, and the claim's date."""
    if at:
        t0 = parse_local(at, site.timezone)
        local_date = t0.astimezone(get_zone(site.timezone)).strftime("%Y-%m-%d")
        return t0, None, local_date

    if not site.lpr_camera_id:
        raise NoLprCamera(
            site.location_name + " has no LPR camera configured. Pass "
            "at=<timestamp> instead, or set lpr_camera_id on its SiteMap."
        )

    date_text = date or today_at_site(site)
    lookup = lookup_plate(
        client, site.lpr_camera_id, plate, date_text, site.timezone, fuzzy
    )
    t0, sighting = resolve_t0(lookup, occurrence)
    return t0, sighting.plate, date_text


def collect(
    client: "SpotAI",
    location: str | int,
    customer: str,
    plate: str | None = None,
    at: str | None = None,
    date: str | None = None,
    claim_ref: str | None = None,
    occurrence: str = "first",
    fuzzy: bool = False,
    reuse_existing: bool = True,
) -> Claim:
    """Package a damage claim into a Spot case. Does not wait for exports."""
    if bool(plate) == bool(at):
        raise ValueError(
            "Pass exactly one of plate= or at= - supplying both would give two "
            "different answers for T0."
        )

    site = client.site_map(location)
    t0, plate_used, date_text = resolve_anchor(
        client, site, plate, at, date, occurrence, fuzzy
    )
    external_id = build_external_id(site.slug, claim_ref, plate_used, date_text)

    integration_id = client.ensure_integration()
    event_type_id = client.ensure_event_type()

    existing = find_device(client, integration_id, external_id, site)
    if existing:
        if not reuse_existing:
            raise ClaimExists("A claim already exists with id " + external_id)
        return claim_from_existing(client, existing, site, t0, external_id)

    # 1. Record first. Identity before work, so nothing is orphaned.
    device = client.create_device(
        integration_id,
        name=device_name(customer, date_text),
        camera_ids=site.key_camera_ids,
        tags=[site.slug.title(), CLAIM_TAG],
        external_id=external_id,
    )
    device_id = int(device["id"])

    # 2. Submit exports. One bad camera must not sink the claim.
    cameras = plan_windows(site, t0)
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

    share_link = make_share_link(client, cameras)

    # 3. Attach the event, carrying enough to rebuild the claim from Spot alone.
    try:
        client.create_event(
            integration_id,
            event_type_id=event_type_id,
            device_id=device_id,
            timestamp=iso_z(t0),
            duration_ms=min(site.transit_seconds * 1000, MAX_EVENT_DURATION_MS),
            attributes={
                "claim_ref": claim_ref or "",
                "plate": plate_used or "",
                "customer": customer,
                "location": site.location_name,
                "share_link": share_link or "",
                "t0": iso_z(t0),
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
        t0=t0,
        device_id=device_id,
        event_id=latest_event_id(client, integration_id, device_id),
        location=site.location_name,
        status=derive_status([c.state for c in cameras]),
        share_link=share_link,
        cameras=cameras,
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
            states.append("FAILED")
            problems.append(
                {"camera": name, "state": "FAILED",
                 "reason": "export was never submitted"}
            )
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
        status=derive_status(states),
        share_link=attrs.get("share_link") or None,
        clips=clips,
        problems=problems,
    )


# ------------------------------------------------------------------ helpers
def make_share_link(client: "SpotAI", cameras: list[ClaimCamera]) -> str | None:
    """One public link covering every camera. Absence must not fail a claim."""
    try:
        start, end = union_span(cameras)
        shared = client.create_shared_search(
            [c.camera_id for c in cameras], start, end
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
    client: "SpotAI", device: dict, site: SiteMap, t0: datetime, external_id: str
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
        t0=t0,
        device_id=device_id,
        event_id=event_id,
        location=site.location_name,
        status=status,
        share_link=share_link,
        reused=True,
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
