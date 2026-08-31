"""HTTP transport for the Spot AI API.

Kept separate from the client so retries, error mapping, and pagination can
be reasoned about and tested without dragging the whole API surface along.

Base URL note: ``https://dev-api.spot.ai`` is the only server listed in Spot's
OpenAPI definition and it serves production data. ``https://api.spot.ai``
returns 404 for ``/v1/*`` paths. The hostname is misleading; this is correct.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import requests

from .errors import (
    SpotAPIError,
    SpotAuthError,
    SpotNotFoundError,
    SpotPermissionError,
)

BASE_URL = "https://dev-api.spot.ai"
USER_AGENT = "spotai-python-wrapper/0.1"


class Transport:
    """Thin HTTP layer: auth, retries, error mapping, cursor pagination."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: int = 30,
        max_retries: int = 4,
    ):
        if not api_key:
            raise SpotAuthError("No API key supplied.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": "Bearer " + api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        url = path if path.startswith("http") else self.base_url + path
        delay = 1.0

        for attempt in range(self.max_retries):
            last = attempt == self.max_retries - 1
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                if last:
                    raise SpotAPIError(
                        "Network error calling " + method + " " + url + ": " + str(exc)
                    ) from exc
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 401:
                raise SpotAuthError("401 Unauthorized - the API key was rejected.")
            if resp.status_code == 403:
                raise SpotPermissionError(
                    "403 Forbidden on " + path + ". The key is valid but lacks "
                    "permission. Check its Role/scope in the Spot AI dashboard."
                )
            if resp.status_code == 404:
                raise SpotNotFoundError("404 Not Found: " + method + " " + path)
            if resp.status_code == 429 or resp.status_code >= 500:
                if last:
                    raise SpotAPIError(
                        str(resp.status_code) + " from " + path + " after "
                        + str(self.max_retries) + " attempts: " + resp.text[:300]
                    )
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else delay)
                delay *= 2
                continue
            if not resp.ok:
                raise SpotAPIError(
                    str(resp.status_code) + " from " + method + " " + path
                    + ": " + resp.text[:300]
                )

            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

        raise SpotAPIError("Request to " + path + " failed.")

    def paginate(
        self, path: str, key: str, params: dict | None = None
    ) -> Iterator[dict]:
        """Walk a cursor-paginated collection until ``next`` is null."""
        params = dict(params or {})
        params.setdefault("limit", 100)
        seen: set[str] = set()
        while True:
            page = self.request("GET", path, params=params) or {}
            for item in page.get(key, []) or []:
                yield item
            cursor = page.get("next")
            # Guard against a server that echoes the same cursor forever.
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
            params["cursor"] = cursor
