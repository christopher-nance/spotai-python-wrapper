"""The SpotAI client - the single public entry point.

Deliberately thin: HTTP lives in ``transport``, the claim workflow lives in
``damage_claims``. This class is the API surface that binds them, so adding an
endpoint is a three-line method and adding a workflow is a new module.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import damage_claims
from .claims import MAX_SHARED_CAMERAS, Claim, ClaimResult
from .errors import NoLprCamera
from .lpr import lookup_plate
from .matching import LIKELY, PlateCandidate, is_usable, rank_candidates
from .sitemap import SiteMap, resolve_site_map
from .transport import BASE_URL, Transport

DEFAULT_INTEGRATION_NAME = "SpotAI Python Wrapper"
DEFAULT_EVENT_TYPE_NAME = "Damage Claim"

SHARED_LINK_MAX_EXPIRY = 604800  # 7 days, Spot's ceiling
DEFAULT_BUFFER_MS = 30000

EVENT_TYPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim_ref": {"type": "string"},
        "plate": {"type": "string"},
        "customer": {"type": "string"},
        "location": {"type": "string"},
        "share_link": {"type": "string"},
        "t0": {"type": "string"},
        "footage_jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "number"},
                    "job_id": {"type": "number"},
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "offset_seconds": {"type": "number"},
                },
            },
        },
    },
}


class SpotAI:
    """Wrapper over the Spot AI REST API.

    Construct it with the site maps for your organisation; the claim methods
    use them to work out which cameras a car passed, and when.

        spot = SpotAI(api_key="zpka_...", site_maps=[wheaton, niles])
        claim = spot.collect_damage_claim(
            location="Wheaton", customer="J. Smith", plate="ABC1234",
        )
    """

    def __init__(
        self,
        api_key: str,
        site_maps: Sequence[SiteMap] | None = None,
        integration_name: str = DEFAULT_INTEGRATION_NAME,
        event_type_name: str = DEFAULT_EVENT_TYPE_NAME,
        base_url: str = BASE_URL,
        timeout: int = 30,
        max_retries: int = 4,
    ):
        self.http = Transport(api_key, base_url, timeout, max_retries)
        self.site_maps: list[SiteMap] = list(site_maps or [])
        self.integration_name = integration_name
        self.event_type_name = event_type_name
        self._integration_id: int | None = None
        self._event_type_id: int | None = None

    # ------------------------------------------------------------- inventory
    def verify_key(self) -> bool:
        """True if the API key is accepted."""
        self.http.request("GET", "/v1/key/verify")
        return True

    def camera_count(self) -> int:
        """Org-wide enabled camera count.

        An aggregate, and *not* filtered by the key's resource scope - which
        makes it the way to spot a key that authenticates but has no Role
        attached: ``camera_count()`` returns a number while ``cameras()``
        returns nothing.
        """
        result = self.http.request("GET", "/v1/cameras/count")
        return int(result) if result is not None else 0

    def locations(self) -> list[dict]:
        return list(self.http.paginate("/v1/locations", "locations"))

    def cameras(self, location_ids: list[int] | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if location_ids:
            params["location_ids"] = location_ids
        return list(self.http.paginate("/v1/cameras", "cameras", params))

    def camera(self, camera_id: int) -> dict:
        return self.http.request("GET", "/v1/cameras/" + str(camera_id))

    def zones(self, camera_id: int) -> Any:
        return self.http.request("GET", "/v1/cameras/" + str(camera_id) + "/zones")

    # ------------------------------------------------------------------- LPR
    def lpr_report(
        self,
        camera_id: int,
        start_iso: str,
        end_iso: str,
        plates: list[str] | None = None,
    ) -> dict:
        """LPR report for one camera over a range.

        Returns per-plate aggregates (visits / first_seen / last_seen), not a
        stream of individual reads.
        """
        params: dict[str, Any] = {"start": start_iso, "end": end_iso}
        if plates:
            params["plates"] = plates
        return self.http.request(
            "GET", "/v1/lpr/cameras/" + str(camera_id) + "/report", params=params
        )

    def interest_lists(self) -> list[dict]:
        return self.http.request("GET", "/v1/lpi") or []

    # --------------------------------------------------------------- footage
    def create_footage_job(
        self, camera_id: int, start_iso: str, end_iso: str
    ) -> dict:
        """Queue an asynchronous historical footage export."""
        return self.http.request(
            "POST",
            "/v1/cameras/" + str(camera_id) + "/footage",
            json_body={"start": start_iso, "end": end_iso},
        )

    def get_footage_job(self, camera_id: int, footage_id: int) -> dict:
        """Poll an export. ``objectPath`` is a signed URL valid for one hour."""
        return self.http.request(
            "GET", "/v1/cameras/" + str(camera_id) + "/footage/" + str(footage_id)
        )

    # --------------------------------------------------------------- sharing
    def create_shared_search(
        self,
        camera_ids: list[int],
        start_iso: str,
        end_iso: str,
        expiry_seconds: int = SHARED_LINK_MAX_EXPIRY,
    ) -> dict:
        """Public multi-camera VOD link. Spot caps this at 16 cameras / 7 days."""
        return self.http.request(
            "POST",
            "/v1/cameras/shared/search",
            json_body={
                "camera_ids": camera_ids[:MAX_SHARED_CAMERAS],
                "start": start_iso,
                "end": end_iso,
                "expiry_in_seconds": min(expiry_seconds, SHARED_LINK_MAX_EXPIRY),
            },
        )

    def create_vod_embed(
        self,
        camera_id: int,
        start_iso: str,
        end_iso: str,
        expires_in: int = SHARED_LINK_MAX_EXPIRY,
    ) -> dict:
        return self.http.request(
            "POST",
            "/v1/embeds/vod",
            json_body={
                "camera_id": camera_id,
                "start": start_iso,
                "end": end_iso,
                "expires_in": expires_in,
            },
        )

    # --------------------------------------------------------- Spot Connect
    def integrations(self) -> list[dict]:
        return (self.http.request("GET", "/v1/integrations") or {}).get(
            "integrations", []
        )

    def devices(
        self, integration_id: int, tags: list[str] | None = None
    ) -> list[dict]:
        params: dict[str, Any] = {}
        if tags:
            params["tags"] = tags
        return list(
            self.http.paginate(
                "/v1/integrations/" + str(integration_id) + "/devices",
                "integration_devices",
                params,
            )
        )

    def create_device(
        self,
        integration_id: int,
        name: str,
        camera_ids: list[int],
        tags: list[str] | None = None,
        external_id: str | None = None,
    ) -> dict:
        """Create an integration device. Spot caps name at 40 chars, cameras at 4."""
        body: dict[str, Any] = {"name": name, "camera_ids": camera_ids}
        if tags:
            body["tags"] = tags
        if external_id:
            body["external_id"] = external_id
        return self.http.request(
            "POST", "/v1/integrations/" + str(integration_id) + "/devices",
            json_body=body,
        )["integration_device"]

    def events(
        self,
        integration_id: int,
        device_ids: list[int] | None = None,
        camera_ids: list[int] | None = None,
    ) -> list[dict]:
        """List integration events.

        Spot returns an empty array unless you filter by device or camera -
        ``start``/``end`` alone is not enough - so one is required here rather
        than silently handing back nothing.
        """
        if not device_ids and not camera_ids:
            raise ValueError(
                "Spot's event list returns nothing without a device or camera "
                "filter; pass device_ids= or camera_ids=."
            )
        params: dict[str, Any] = {}
        if device_ids:
            params["integrationDeviceIds"] = device_ids
        if camera_ids:
            params["cameraIds"] = camera_ids
        return (
            self.http.request(
                "GET", "/v1/integrations/" + str(integration_id) + "/events",
                params=params,
            )
            or {}
        ).get("integration_events", [])

    def create_event(
        self,
        integration_id: int,
        event_type_id: int,
        device_id: int,
        timestamp: str,
        attributes: dict[str, Any],
        duration_ms: int = 240000,
        buffer_ms: int = DEFAULT_BUFFER_MS,
    ) -> None:
        """Ingest an event. Spot answers 202 and processes it asynchronously."""
        self.http.request(
            "POST", "/v1/integrations/" + str(integration_id) + "/events",
            json_body={
                "integration_event_type_id": event_type_id,
                "integration_device_id": device_id,
                "timestamp": timestamp,
                "duration": duration_ms,
                "buffer": buffer_ms,
                "attributes": attributes,
            },
        )

    def ensure_integration(self) -> int:
        """Find or create the wrapper's integration. Idempotent, cached."""
        if self._integration_id is not None:
            return self._integration_id
        for integ in self.integrations():
            if integ.get("name") == self.integration_name:
                self._integration_id = int(integ["id"])
                return self._integration_id
        created = self.http.request(
            "POST", "/v1/integrations",
            json_body={"name": self.integration_name, "integration_type": "custom"},
        )
        self._integration_id = int(created["integration"]["id"])
        return self._integration_id

    def ensure_event_type(self) -> int:
        """Find or create the claim event type. Idempotent, cached."""
        if self._event_type_id is not None:
            return self._event_type_id
        integration_id = self.ensure_integration()
        existing = self.http.paginate(
            "/v1/integrations/" + str(integration_id) + "/event-types",
            "integration_event_types",
        )
        for event_type in existing:
            if event_type.get("name") == self.event_type_name:
                self._event_type_id = int(event_type["id"])
                return self._event_type_id
        created = self.http.request(
            "POST", "/v1/integrations/" + str(integration_id) + "/event-types",
            json_body={
                "name": self.event_type_name,
                "schema": EVENT_TYPE_SCHEMA,
                "duration": 240000,
                "buffer": DEFAULT_BUFFER_MS,
            },
        )
        self._event_type_id = int(created["integration_event_type"]["id"])
        return self._event_type_id

    # ------------------------------------------------------------- workflow
    def site_map(self, location: str | int) -> SiteMap:
        """Find a configured site by id, exact name, or unambiguous substring."""
        return resolve_site_map(self.site_maps, location)

    def match_plate(
        self,
        location: str | int,
        plate: str,
        date: str | None = None,
        limit: int = 5,
    ) -> list[PlateCandidate]:
        """Rank what the LPR actually read against a typed plate.

        Exact matching is not enough: measured on live data, 46% of reads are
        shorter than a full plate because characters are lost from the left.
        This scores every read for the day and returns ranked candidates with
        confidence, so a caller can auto-accept a strong match or show a
        person the shortlist.

        Returns an empty list when the plate is unusable (``N/A``, ``TEST``, a
        pasted note) or nothing scores above the floor - which is the right
        answer when the car was never read.
        """
        site = self.site_map(location)
        if not site.lpr_camera_id:
            raise NoLprCamera(site.location_name + " has no LPR camera.")
        if not is_usable(plate):
            return []
        day = date or damage_claims.today_at_site(site)
        lookup = lookup_plate(
            self, site.lpr_camera_id, plate, day, site.timezone, fuzzy=False
        )
        return rank_candidates(plate, lookup.sightings, limit=limit)

    def collect_damage_claim(
        self,
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
        estimate_window_minutes: int = 20,
    ) -> Claim:
        """Package a damage claim into a Spot case. Returns immediately.

        Supply ``plate``, ``at`` (a site-local timestamp), or **both** - the
        plate is preferred and the timestamp is the fallback when no confident
        LPR match exists. That combination is the normal case: only some sites
        have a working LPR camera, and roughly a quarter of claims arrive with
        no plate at all.

        ``clips`` decides whether footage is exported. Under ``"auto"``, an
        exact LPR match gets narrow clips, while a typed estimate gets a wide
        scrubbable link instead - because a 90-second clip centred on someone's
        recollection often misses the car, and a clip of the wrong car is worse
        than no clip.
        """
        return damage_claims.collect(
            self, location, customer, plate, at, date, claim_ref,
            occurrence, reuse_existing, clips, min_confidence,
            estimate_window_minutes,
        )

    def get_claim(
        self, device_id: int, event_id: str | None = None
    ) -> ClaimResult:
        """Current state of a claim: status, clips, and anything that failed.

        Clip URLs are re-signed on every call and live one hour. Use them
        immediately; never store one.
        """
        return damage_claims.fetch(self, device_id, event_id)
