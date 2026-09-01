"""Site and camera configuration.

The Spot AI camera object carries no position or sequence field, so the
physical order of cameras through a wash tunnel cannot be derived from the
API. It has to be supplied. A SiteMap is that supplied order.

This library ships no site data of its own - the caller owns it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .errors import SiteMapNotFound
from .timewin import seed_offsets

DEFAULT_TRANSIT_SECONDS = 240
DEFAULT_CLIP_SECONDS = 120
DEFAULT_PAD_BEFORE = 30
DEFAULT_PAD_AFTER = 60
DEFAULT_TIMEZONE = "America/Chicago"

MAX_DEVICE_CAMERAS = 4
ROLES = ("entry", "tunnel", "exit")


@dataclass
class Camera:
    """One camera in the tunnel, in physical order.

    ``offset_seconds`` is how long after T0 this camera sees the car. Leave it
    as None and the SiteMap seeds it from ``transit_seconds``.
    """

    id: int
    name: str = ""
    role: str = "tunnel"
    offset_seconds: int | None = None

    def __post_init__(self) -> None:
        self.id = int(self.id)
        if self.role not in ROLES:
            raise ValueError(
                "Camera role must be one of " + ", ".join(ROLES)
                + ", got " + repr(self.role)
            )
        if self.offset_seconds is not None and self.offset_seconds < 0:
            raise ValueError("offset_seconds must be >= 0")
        if not self.name:
            self.name = "camera-" + str(self.id)


@dataclass
class SiteMap:
    """The ordered camera map for one wash location."""

    location_id: int
    location_name: str
    cameras: list[Camera]
    timezone: str = DEFAULT_TIMEZONE
    transit_seconds: int = DEFAULT_TRANSIT_SECONDS
    clip_seconds: int = DEFAULT_CLIP_SECONDS
    pad_before_seconds: int = DEFAULT_PAD_BEFORE
    pad_after_seconds: int = DEFAULT_PAD_AFTER
    lpr_camera_id: int | None = None
    key_camera_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.location_id = int(self.location_id)
        self.cameras = [
            c if isinstance(c, Camera) else Camera(**c) for c in self.cameras
        ]
        if not self.cameras:
            raise ValueError("A SiteMap needs at least one camera.")
        if self.clip_seconds <= 0:
            raise ValueError("clip_seconds must be greater than 0.")
        if self.pad_before_seconds < 0 or self.pad_after_seconds < 0:
            raise ValueError("padding must be >= 0")

        dupes = _duplicates(c.id for c in self.cameras)
        if dupes:
            raise ValueError(
                "Camera id(s) repeated in the site map: "
                + ", ".join(str(d) for d in dupes)
            )

        self._seed_offsets()
        if not self.key_camera_ids:
            self.key_camera_ids = self.default_key_cameras()
        self._validate_key_cameras()

    # -- offsets --------------------------------------------------------
    def _seed_offsets(self) -> None:
        """Fill in any offsets the caller left as None.

        Entry cameras sit at 0, exit cameras at ``transit_seconds``, and
        tunnel cameras spread evenly between the two.
        """
        tunnel = [c for c in self.cameras if c.role == "tunnel"]
        seeded = seed_offsets(len(tunnel), self.transit_seconds)
        for cam, value in zip(tunnel, seeded):
            if cam.offset_seconds is None:
                cam.offset_seconds = value
        for cam in self.cameras:
            if cam.offset_seconds is None:
                cam.offset_seconds = 0 if cam.role == "entry" else self.transit_seconds

    # -- cameras --------------------------------------------------------
    def ordered_cameras(self) -> list[Camera]:
        """Every camera, in the order the car passes them."""
        return sorted(self.cameras, key=lambda c: (c.offset_seconds or 0))

    def camera_ids(self) -> list[int]:
        return [c.id for c in self.ordered_cameras()]

    def all_camera_ids(self) -> list[int]:
        """Every camera, for a claim that wants all of them on devices.

        Set ``key_camera_ids=site.all_camera_ids()`` to have every camera
        surface natively in Spot; the collector will create one device per
        four.
        """
        return self.camera_ids()

    def default_key_cameras(self) -> list[int]:
        """The four most probative cameras, for a single-device claim.

        A Spot integration device accepts at most four cameras. This is the
        default; pass a longer ``key_camera_ids`` to span several devices.
        """
        picks: list[int] = []
        for role in ROLES:
            for cam in self.ordered_cameras():
                if cam.role == role:
                    picks.append(cam.id)
                    break
        last = self.ordered_cameras()[-1].id
        if last not in picks:
            picks.append(last)
        # Preserve order, drop duplicates, respect Spot's hard cap.
        seen: set[int] = set()
        out = [c for c in picks if not (c in seen or seen.add(c))]
        return out[:MAX_DEVICE_CAMERAS]

    def _validate_key_cameras(self) -> None:
        known = {c.id for c in self.cameras}
        unknown = [c for c in self.key_camera_ids if c not in known]
        if unknown:
            raise ValueError(
                "key_camera_ids references cameras not in this site map: "
                + ", ".join(str(u) for u in unknown)
            )
        # No cap here on purpose. Spot allows 4 cameras per *device*, but a
        # claim may span several devices, so any number is legal - the
        # collector chunks them.

    @property
    def slug(self) -> str:
        """Short uppercase identifier used in a claim's external_id.

        "Example Wash: Wheaton" -> "WHEATON"
        """
        name = self.location_name
        tail = name.split(":")[-1] if ":" in name else name
        return re.sub(r"[^A-Za-z0-9]+", "_", tail).strip("_").upper()

    # -- serialisation --------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteMap":
        payload = dict(data)
        payload["cameras"] = [
            c if isinstance(c, Camera) else Camera(**c)
            for c in payload.get("cameras", [])
        ]
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "location_name": self.location_name,
            "timezone": self.timezone,
            "transit_seconds": self.transit_seconds,
            "clip_seconds": self.clip_seconds,
            "pad_before_seconds": self.pad_before_seconds,
            "pad_after_seconds": self.pad_after_seconds,
            "lpr_camera_id": self.lpr_camera_id,
            "key_camera_ids": list(self.key_camera_ids),
            "cameras": [
                {
                    "id": c.id,
                    "name": c.name,
                    "role": c.role,
                    "offset_seconds": c.offset_seconds,
                }
                for c in self.ordered_cameras()
            ],
        }


def _duplicates(values: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    dupes: list[int] = []
    for v in values:
        if v in seen and v not in dupes:
            dupes.append(v)
        seen.add(v)
    return dupes


def resolve_site_map(
    site_maps: Sequence[SiteMap], location: str | int
) -> SiteMap:
    """Find a site map by id, exact name, or unambiguous substring.

    A substring matching more than one site raises rather than guessing -
    picking the wrong site would clip footage from the wrong building.
    """
    if not site_maps:
        raise SiteMapNotFound(
            "No site maps configured. Pass site_maps=[...] when constructing "
            "SpotAI."
        )

    if isinstance(location, int) or str(location).isdigit():
        wanted = int(location)
        for sm in site_maps:
            if sm.location_id == wanted:
                return sm
        raise SiteMapNotFound(
            "No site map with location_id " + str(wanted) + ". Known: "
            + ", ".join(str(s.location_id) for s in site_maps)
        )

    text = str(location).strip()
    for sm in site_maps:
        if sm.location_name.lower() == text.lower():
            return sm

    matches = [sm for sm in site_maps if text.lower() in sm.location_name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SiteMapNotFound(
            repr(text) + " matches more than one site: "
            + ", ".join(m.location_name for m in matches)
            + ". Use the exact name or the location_id."
        )
    raise SiteMapNotFound(
        "No site map matching " + repr(text) + ". Known sites: "
        + ", ".join(s.location_name for s in site_maps)
    )
