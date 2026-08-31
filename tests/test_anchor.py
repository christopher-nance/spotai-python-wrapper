"""Tests for T0 resolution and the site-local date used in a claim's identity.

The date derived here ends up inside the device name and the external_id, so
getting it wrong silently mis-identifies a claim.
"""

from datetime import datetime, timezone

import pytest

from spotai import Camera, SiteMap
from spotai.damage_claims import resolve_anchor
from spotai.errors import NoLprCamera


def site(tz="America/Chicago", lpr=None):
    return SiteMap(
        location_id=1,
        location_name="Example Wash: Riverside",
        timezone=tz,
        lpr_camera_id=lpr,
        cameras=[
            Camera(id=1, name="LPR", role="entry"),
            Camera(id=2, name="Exit", role="exit"),
        ],
    )


class TestTimestampPath:
    def test_converts_site_local_time_to_utc(self):
        t0, plate, date_text = resolve_anchor(
            None, site(), None, "2026-08-30 10:09", None, "first", False
        )
        assert t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)
        assert plate is None

    def test_date_is_the_sites_date_not_the_servers(self):
        # 23:30 in Chicago is already 04:30 UTC the next day. The claim
        # belongs to the 30th at that site, whatever timezone the server is
        # in - this date goes into the device name and the external_id.
        _, _, date_text = resolve_anchor(
            None, site(), None, "2026-08-30 23:30", None, "first", False
        )
        assert date_text == "2026-08-30"

    def test_date_matches_site_across_a_different_zone(self):
        _, _, date_text = resolve_anchor(
            None, site(tz="Pacific/Honolulu"), None, "2026-08-30 20:00",
            None, "first", False,
        )
        assert date_text == "2026-08-30"

    def test_explicit_utc_input_is_respected(self):
        t0, _, _ = resolve_anchor(
            None, site(), None, "2026-08-30T15:09:00Z", None, "first", False
        )
        assert t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)


class TestPlatePathGuards:
    def test_site_without_lpr_camera_raises_clearly(self):
        with pytest.raises(NoLprCamera, match="no LPR camera"):
            resolve_anchor(
                None, site(lpr=None), "ABC1234", None, None, "first", False
            )

    def test_error_names_the_site_and_the_way_out(self):
        with pytest.raises(NoLprCamera) as exc:
            resolve_anchor(
                None, site(lpr=None), "ABC1234", None, None, "first", False
            )
        message = str(exc.value)
        assert "Riverside" in message
        assert "at=" in message


class TestPlateLookup:
    class FakeClient:
        """Minimal stand-in - resolve_anchor only calls lpr_report."""

        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        def lpr_report(self, camera_id, start, end, plates=None):
            self.calls.append({"camera_id": camera_id, "plates": plates})
            return {"plates": self.rows}

    def test_uses_first_seen_by_default(self):
        client = self.FakeClient(
            [{"plate": "ABC1234", "visits": 1,
              "first_seen": "2026-08-30T15:09:00.000Z",
              "last_seen": "2026-08-30T15:10:00.000Z"}]
        )
        t0, plate, _ = resolve_anchor(
            client, site(lpr=99), "ABC1234", None, "2026-08-30", "first", False
        )
        assert t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)
        assert plate == "ABC1234"

    def test_last_occurrence_uses_last_seen(self):
        client = self.FakeClient(
            [{"plate": "ABC1234", "visits": 1,
              "first_seen": "2026-08-30T15:09:00.000Z",
              "last_seen": "2026-08-30T15:10:00.000Z"}]
        )
        t0, _, _ = resolve_anchor(
            client, site(lpr=99), "ABC1234", None, "2026-08-30", "last", False
        )
        assert t0 == datetime(2026, 8, 30, 15, 10, tzinfo=timezone.utc)

    def test_queries_the_sites_lpr_camera(self):
        client = self.FakeClient(
            [{"plate": "A", "visits": 1,
              "first_seen": "2026-08-30T15:09:00.000Z",
              "last_seen": "2026-08-30T15:09:30.000Z"}]
        )
        resolve_anchor(client, site(lpr=2001), "A", None, "2026-08-30",
                       "first", False)
        assert client.calls[0]["camera_id"] == 2001

    def test_fuzzy_submits_lookalike_variants(self):
        client = self.FakeClient(
            [{"plate": "8B00000", "visits": 1,
              "first_seen": "2026-08-30T15:09:00.000Z",
              "last_seen": "2026-08-30T15:09:30.000Z"}]
        )
        resolve_anchor(client, site(lpr=1), "8B00000", None, "2026-08-30",
                       "first", True)
        submitted = client.calls[0]["plates"]
        assert "8B00000" in submitted and len(submitted) > 1

    def test_non_fuzzy_submits_only_the_plate(self):
        client = self.FakeClient(
            [{"plate": "8B00000", "visits": 1,
              "first_seen": "2026-08-30T15:09:00.000Z",
              "last_seen": "2026-08-30T15:09:30.000Z"}]
        )
        resolve_anchor(client, site(lpr=1), "8B00000", None, "2026-08-30",
                       "first", False)
        assert client.calls[0]["plates"] == ["8B00000"]
