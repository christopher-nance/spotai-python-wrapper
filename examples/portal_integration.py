"""Reference scaffold: wiring this wrapper into a Flask + Firebase portal.

Drop-in shapes for the three pieces you need. Site-specific numbers (camera
order, offsets, transit times) deliberately come from config, so none of this
changes when cameras are moved or re-angled.

The design rule: **claim submission never waits on Spot AI.** It writes a job
record and returns. A separate worker drains the queue.

Why not a thread in the request? Multiple gunicorn workers would each run it,
it dies on deploy, it cannot retry, and it is invisible when it fails. The
worker-process pattern already used for other schedulers is the right shape.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from spotai import SiteMap, SpotAI
from spotai.errors import SpotError

# ---------------------------------------------------------------------------
# 1. Site maps come from config, not code
# ---------------------------------------------------------------------------
# Store these in Firebase alongside each site record, e.g.
#   GeneralSettings/Sites/<SiteName>/SPOTAI_SITE_MAP
# so a camera move is a config edit, not a deploy.
#
# The portal's incident_location is the plain site name ("Wheaton", "Burbank"),
# which resolves directly - no mapping table needed.


def load_site_maps(get_rtdb_data) -> list[SiteMap]:
    """Build SiteMaps from the Firebase Sites config."""
    sites = get_rtdb_data("GeneralSettings/Sites") or {}
    maps = []
    for name, record in sites.items():
        raw = record.get("SPOTAI_SITE_MAP")
        if not raw:
            continue                      # site not mapped yet - skip quietly
        data = json.loads(raw) if isinstance(raw, str) else raw
        data.setdefault("location_name", name)
        maps.append(SiteMap.from_dict(data))
    return maps


def build_client(api_key: str, get_rtdb_data) -> SpotAI:
    """Construct once at startup, never per request."""
    return SpotAI(api_key=api_key, site_maps=load_site_maps(get_rtdb_data))


# ---------------------------------------------------------------------------
# 2. On submission: write a job, return immediately
# ---------------------------------------------------------------------------
JOB_PATH = "DamageClaims/{claim_id}/cameraJob"


def enqueue_camera_job(set_rtdb_data, claim_id: str, claim_data: dict) -> None:
    """Called from the claim wizard AFTER validation passes.

    Costs one small Firebase write. Does not import or contact Spot AI, so a
    Spot outage, a slow export queue, or an expired key cannot affect claim
    submission.
    """
    incident = claim_data.get("incident", {})
    vehicle = claim_data.get("vehicle", {})
    set_rtdb_data(
        JOB_PATH.format(claim_id=claim_id),
        {
            "status": "queued",
            "requestedAt": datetime.now(timezone.utc).isoformat(),
            "location": incident.get("location", ""),
            "plate": vehicle.get("licensePlate", ""),
            "incidentDateTime": incident.get("dateTime", ""),
            "customer": claim_data.get("customer", {}).get("name", ""),
            "attempts": 0,
            "lastError": None,
        },
    )


# ---------------------------------------------------------------------------
# 3. The worker: drains the queue
# ---------------------------------------------------------------------------
MAX_ATTEMPTS = 3
POLL_SECONDS = 45


def normalise_incident_datetime(value: str) -> str | None:
    """The export contains three formats; one of them is free text.

    795 records are 'YYYY-MM-DDTHH:MM', 188 carry a UTC offset, and one is
    'Monday, April 7, 2026 @ 3:00PM'. The wrapper parses the first two.
    """
    if not value:
        return None
    v = value.strip().replace("T", " ")
    if len(v) >= 16 and v[:4].isdigit():
        return v[:16]
    return None      # unparseable legacy value - let the caller decide


def process_one(spot: SpotAI, claim_id: str, job: dict, save) -> None:
    """Run one queued job and write the outcome back onto the claim."""
    at = normalise_incident_datetime(job.get("incidentDateTime", ""))
    plate = (job.get("plate") or "").strip() or None

    if not plate and not at:
        save(claim_id, {"status": "failed",
                        "lastError": "no plate and no usable incident time"})
        return

    try:
        claim = spot.collect_damage_claim(
            location=job["location"],
            customer=job.get("customer") or "Unknown",
            plate=plate,           # preferred anchor
            at=at,                 # fallback when the plate cannot be matched
            claim_ref=claim_id,    # makes the claim idempotent on re-run
        )
    except SpotError as exc:
        attempts = int(job.get("attempts", 0)) + 1
        save(claim_id, {
            "status": "failed" if attempts >= MAX_ATTEMPTS else "queued",
            "attempts": attempts,
            "lastError": str(exc)[:500],
        })
        return

    # Store what is needed to re-fetch. Never store clip URLs - one hour.
    save(claim_id, {
        "status": claim.status,
        "spotDeviceId": claim.device_id,
        "spotEventId": claim.event_id,
        "shareLink": claim.share_link,
        "anchor": claim.anchor,                       # plate | estimate
        "matchedPlate": claim.matched_plate,
        "matchConfidence": claim.match_confidence,
        "needsReview": claim.needs_review,
        "candidates": claim.candidates,               # shortlist for a human
        "lastError": None,
    })


def worker_loop(spot: SpotAI, next_queued, save, stop=lambda: False) -> None:
    """Run in its own process, like the other schedulers.

    Because the wrapper is idempotent on its external_id, a crashed or retried
    job cannot create duplicate cases.
    """
    while not stop():
        job = next_queued()
        if not job:
            time.sleep(POLL_SECONDS)
            continue
        claim_id, payload = job
        save(claim_id, {"status": "running"})
        process_one(spot, claim_id, payload, save)


# ---------------------------------------------------------------------------
# 4. On claim view: always re-fetch
# ---------------------------------------------------------------------------
def camera_evidence(spot: SpotAI, stored: dict) -> dict[str, Any]:
    """Build the view model for a claim page.

    Clip URLs are re-signed on every call and live one hour, so they are
    fetched here and used immediately - never cached.
    """
    if not stored or not stored.get("spotDeviceId"):
        return {"state": stored.get("status", "none") if stored else "none"}

    try:
        result = spot.get_claim(stored["spotDeviceId"], stored.get("spotEventId"))
    except SpotError as exc:
        return {"state": "error", "message": str(exc)}

    return {
        "state": result.status,             # pending|ready|partial|failed|link-only
        "share_link": result.share_link,
        "clips": [
            {"name": c.name, "role": c.role, "url": c.url,
             "expires": c.url_expires.isoformat() if c.url_expires else None}
            for c in result.clips
        ],
        "problems": result.problems,
        "anchor": result.anchor,
        "matched_plate": result.matched_plate,
        "needs_review": stored.get("needsReview", False),
        "candidates": stored.get("candidates", []),
    }


# ---------------------------------------------------------------------------
# 5. Extending the portal's CameraCaseLink
# ---------------------------------------------------------------------------
# The existing dataclass holds only caseURL and primaryCase. Add these, all
# optional so existing records keep loading unchanged:
#
#     spot_device_id:   Optional[int]   -> "spotDeviceId"
#     spot_event_id:    Optional[str]   -> "spotEventId"
#     status:           str = "pending" -> "status"
#     anchor:           str = "plate"   -> "anchor"
#     matched_plate:    Optional[str]   -> "matchedPlate"
#     match_confidence: Optional[float] -> "matchConfidence"
#
# Keep the manual POST /api/claims/<id>/camera-cases route. Automation will
# miss - retention gaps, wedged exports, cars the reader never saw - and a
# person must always be able to paste a URL.
