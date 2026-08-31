"""Exception hierarchy for the Spot AI wrapper."""

from __future__ import annotations


class SpotError(Exception):
    """Base class for every error raised by this library."""


# -- transport / HTTP ---------------------------------------------------
class SpotAuthError(SpotError):
    """401 - the API key is missing, malformed, or revoked."""


class SpotPermissionError(SpotError):
    """403 - the key authenticates but lacks permission for this resource."""


class SpotNotFoundError(SpotError):
    """404 - the resource does not exist."""


class SpotAPIError(SpotError):
    """Any other non-success response from the API."""


# -- domain -------------------------------------------------------------
class SiteMapNotFound(SpotError):
    """No configured SiteMap matched the requested location."""


class PlateNotFound(SpotError):
    """No LPR reads matched the plate in the requested window."""


class NoLprCamera(SpotError):
    """The site has no LPR camera and no explicit timestamp was supplied."""


class ClaimExists(SpotError):
    """A claim with this external_id already exists.

    Only raised when idempotent reuse is explicitly disabled; by default
    ``collect_damage_claim`` returns the existing claim instead.
    """
