"""Python wrapper for the Spot AI REST API.

Unofficial. Not affiliated with, endorsed by, or supported by Spot AI.

Exposes the Spot AI endpoints as ordinary methods, and adds one composite
operation Spot does not provide: turning a damage claim into a packaged,
footage-linked case.

    from spotai import SpotAI, SiteMap, Camera

    spot = SpotAI(api_key="zpka_...", site_maps=[...])
    claim = spot.collect_damage_claim(
        location="Wheaton", customer="J. Smith", plate="ABC1234",
    )
    result = spot.get_claim(claim.device_id, claim.event_id)
"""

from .claims import Claim, ClaimResult, Clip
from .client import SpotAI
from .errors import (
    ClaimExists,
    NoLprCamera,
    PlateNotFound,
    SiteMapNotFound,
    SpotAPIError,
    SpotAuthError,
    SpotError,
    SpotNotFoundError,
    SpotPermissionError,
)
from .sitemap import Camera, SiteMap

__version__ = "0.1.0"

__all__ = [
    "SpotAI",
    "SiteMap",
    "Camera",
    "Claim",
    "ClaimResult",
    "Clip",
    "SpotError",
    "SpotAuthError",
    "SpotPermissionError",
    "SpotNotFoundError",
    "SpotAPIError",
    "SiteMapNotFound",
    "PlateNotFound",
    "NoLprCamera",
    "ClaimExists",
    "__version__",
]
