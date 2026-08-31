"""Tests for T0 resolution: where it came from and how much to trust it.

The date derived here ends up inside the device name and the external_id, so
getting it wrong silently mis-identifies a claim. The *anchor kind* decides
whether narrow clips or a wide scrubbable link get produced.
"""

from datetime import datetime, timezone

import pytest

from spotai import Camera, SiteMap
from spotai.damage_claims import ANCHOR_ESTIMATE, ANCHOR_PLATE, resolve_anchor
from spotai.errors import NoLprCamera, PlateNotFound


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


class FakeClient:
    """resolve_anchor only ever calls lpr_report."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def lpr_report(self, camera_id, start, end, plates=None):
        self.calls.append({"camera_id": camera_id, "plates": plates})
        return {"plates": self.rows}


def read(plate, hh=15, mm=9):
    return {
        "plate": plate,
        "visits": 1,
        "first_seen": f"2026-08-30T{hh:02d}:{mm:02d}:00.000Z",
        "last_seen": f"2026-08-30T{hh:02d}:{mm + 1:02d}:00.000Z",
    }


class TestTimestampAnchor:
    def test_converts_site_local_time_to_utc(self):
        a = resolve_anchor(None, site(), None, "2026-08-30 10:09", None, "first")
        assert a.t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)

    def test_is_marked_as_an_estimate(self):
        # A typed time is a person's recollection, not a measurement.
        a = resolve_anchor(None, site(), None, "2026-08-30 10:09", None, "first")
        assert a.kind == ANCHOR_ESTIMATE
        assert not a.precise

    def test_date_is_the_sites_date_not_the_servers(self):
        # 23:30 in Chicago is already the next day in UTC; the claim belongs
        # to the 30th at that site whatever the server's timezone is.
        a = resolve_anchor(None, site(), None, "2026-08-30 23:30", None, "first")
        assert a.date_text == "2026-08-30"

    def test_date_matches_site_in_another_zone(self):
        a = resolve_anchor(
            None, site(tz="Pacific/Honolulu"), None, "2026-08-30 20:00",
            None, "first",
        )
        assert a.date_text == "2026-08-30"

    def test_explicit_utc_input_is_respected(self):
        a = resolve_anchor(None, site(), None, "2026-08-30T15:09:00Z", None, "first")
        assert a.t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)


class TestPlateAnchor:
    def test_confident_match_is_precise(self):
        client = FakeClient([read("AB12345")])
        a = resolve_anchor(client, site(lpr=99), "AB12345", None, "2026-08-30", "first")
        assert a.kind == ANCHOR_PLATE
        assert a.precise
        assert a.plate == "AB12345"

    def test_uses_first_seen_by_default(self):
        client = FakeClient([read("AB12345", 15, 9)])
        a = resolve_anchor(client, site(lpr=99), "AB12345", None, "2026-08-30", "first")
        assert a.t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)

    def test_last_occurrence_uses_last_seen(self):
        client = FakeClient([read("AB12345", 15, 9)])
        a = resolve_anchor(client, site(lpr=99), "AB12345", None, "2026-08-30", "last")
        assert a.t0 == datetime(2026, 8, 30, 15, 10, tzinfo=timezone.utc)

    def test_queries_the_sites_lpr_camera(self):
        client = FakeClient([read("AB12345")])
        resolve_anchor(client, site(lpr=2001), "AB12345", None, "2026-08-30", "first")
        assert client.calls[0]["camera_id"] == 2001

    def test_truncated_read_still_matches(self):
        # The dominant real failure: the LPR only read "12345".
        client = FakeClient([read("12345")])
        a = resolve_anchor(client, site(lpr=99), "AB12345", None, "2026-08-30", "first")
        assert a.kind == ANCHOR_PLATE
        assert a.plate == "12345"
        assert a.confidence >= 0.78

    def test_records_the_confidence(self):
        client = FakeClient([read("AB12345")])
        a = resolve_anchor(client, site(lpr=99), "AB12345", None, "2026-08-30", "first")
        assert a.confidence == 1.0


class TestFallback:
    def test_unmatched_plate_falls_back_to_the_typed_time(self):
        # The normal portal case: both supplied, plate preferred.
        client = FakeClient([read("QQ11111")])
        a = resolve_anchor(
            client, site(lpr=99), "AB12345", "2026-08-30 10:09", None, "first"
        )
        assert a.kind == ANCHOR_ESTIMATE
        assert a.t0 == datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)

    def test_confident_match_wins_over_the_typed_time(self):
        client = FakeClient([read("AB12345", 16, 30)])
        a = resolve_anchor(
            client, site(lpr=99), "AB12345", "2026-08-30 10:09", None, "first"
        )
        assert a.kind == ANCHOR_PLATE
        assert a.t0 == datetime(2026, 8, 30, 16, 30, tzinfo=timezone.utc)

    def test_junk_plate_falls_back_without_querying(self):
        client = FakeClient([read("AB12345")])
        a = resolve_anchor(
            client, site(lpr=99), "N/A", "2026-08-30 10:09", None, "first"
        )
        assert a.kind == ANCHOR_ESTIMATE
        assert client.calls == []          # never wasted a call on "N/A"

    def test_site_without_lpr_uses_the_time_directly(self):
        a = resolve_anchor(
            None, site(lpr=None), "AB12345", "2026-08-30 10:09", None, "first"
        )
        assert a.kind == ANCHOR_ESTIMATE


class TestFailures:
    def test_no_lpr_and_no_time_raises(self):
        with pytest.raises(NoLprCamera, match="no LPR camera"):
            resolve_anchor(None, site(lpr=None), "AB12345", None, None, "first")

    def test_unmatched_plate_with_no_fallback_raises(self):
        client = FakeClient([read("QQ11111")])
        with pytest.raises(PlateNotFound, match="No confident LPR match"):
            resolve_anchor(client, site(lpr=99), "AB12345", None, "2026-08-30", "first")

    def test_failure_carries_the_candidates_for_a_human(self):
        client = FakeClient([read("AB12346")])   # one character off
        with pytest.raises(PlateNotFound) as exc:
            resolve_anchor(
                client, site(lpr=99), "AB12345", None, "2026-08-30", "first",
                min_confidence=0.99,
            )
        assert exc.value.candidates
        assert exc.value.candidates[0].plate == "AB12346"

    def test_neither_plate_nor_time_raises(self):
        with pytest.raises(ValueError, match="plate= or at="):
            resolve_anchor(None, site(lpr=99), None, None, None, "first")
