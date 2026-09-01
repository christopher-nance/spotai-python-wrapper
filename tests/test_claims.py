"""Tests for claim naming, identity, status, and window planning."""

from datetime import datetime, timezone

import pytest

from spotai import Camera, SiteMap
from spotai.claims import (
    MAX_DEVICE_NAME,
    build_external_id,
    chunk_cameras,
    device_name_part,
    part_external_id,
    derive_status,
    device_name,
    plan_windows,
    select_share_cameras,
    signed_url_expiry,
    union_span,
)
from spotai.lpr import fuzzy_variants, normalize_plate


class TestDeviceName:
    def test_basic_format(self):
        assert device_name("J.Smith", "2026-08-30") == "J.Smith | 2026-08-30"

    def test_fits_spot_forty_character_cap(self):
        name = device_name("A" * 100, "2026-08-30")
        assert len(name) <= MAX_DEVICE_NAME

    def test_date_survives_truncation(self):
        # The date is what distinguishes a repeat customer's two claims.
        name = device_name("Bartholomew Fotheringay-Smythe III", "2026-08-30")
        assert name.endswith("| 2026-08-30")
        assert len(name) <= MAX_DEVICE_NAME

    def test_long_name_is_truncated_not_rejected(self):
        name = device_name("A" * 100, "2026-08-30")
        assert name.startswith("A")
        assert name.endswith(" | 2026-08-30")

    def test_empty_customer_falls_back(self):
        assert device_name("", "2026-08-30") == "Unknown | 2026-08-30"

    def test_whitespace_customer_falls_back(self):
        assert device_name("   ", "2026-08-30") == "Unknown | 2026-08-30"

    def test_strips_surrounding_whitespace(self):
        assert device_name("  J.Smith  ", "2026-08-30") == "J.Smith | 2026-08-30"

    def test_exactly_at_the_boundary(self):
        # 27 chars of customer + 13 of suffix = exactly 40
        name = device_name("B" * 27, "2026-08-30")
        assert len(name) == MAX_DEVICE_NAME


class TestExternalId:
    def test_full_form(self):
        assert build_external_id("WHEATON", "CLAIM-118", "8B00000", "2026-08-30") == (
            "WHEATON:CLAIM-118:8B00000:2026-08-30"
        )

    def test_missing_ref_and_plate_use_sentinels(self):
        got = build_external_id("NILES", None, None, "2026-08-30")
        assert got == "NILES:NOREF:NOPLATE:2026-08-30"

    def test_plate_is_upper_cased(self):
        got = build_external_id("WHEATON", "C1", "abc1234", "2026-08-30")
        assert "ABC1234" in got

    def test_colons_in_input_do_not_break_the_key(self):
        got = build_external_id("WHEATON", "A:B", "P:Q", "2026-08-30")
        assert got.count(":") == 3

    def test_respects_length_cap(self):
        got = build_external_id("X" * 300, "Y" * 300, "Z" * 300, "2026-08-30")
        assert len(got) <= 255

    def test_is_deterministic(self):
        a = build_external_id("WHEATON", "C1", "ABC1234", "2026-08-30")
        b = build_external_id("WHEATON", "C1", "ABC1234", "2026-08-30")
        assert a == b


class TestDeriveStatus:
    def test_all_succeeded_is_ready(self):
        assert derive_status(["SUCCEEDED"] * 3) == "ready"

    def test_any_queued_is_pending(self):
        assert derive_status(["SUCCEEDED", "QUEUED"]) == "pending"

    def test_any_processing_is_pending(self):
        assert derive_status(["SUCCEEDED", "PROCESSING"]) == "pending"

    def test_pending_wins_over_failure(self):
        # Still in flight, so not yet a final answer.
        assert derive_status(["FAILED", "PROCESSING"]) == "pending"

    def test_mixed_terminal_is_partial(self):
        assert derive_status(["SUCCEEDED", "FAILED"]) == "partial"

    def test_stalled_counts_as_failure(self):
        assert derive_status(["SUCCEEDED", "STALLED"]) == "partial"

    def test_none_succeeded_is_failed(self):
        assert derive_status(["FAILED", "STALLED"]) == "failed"

    def test_empty_is_failed(self):
        assert derive_status([]) == "failed"

    def test_realistic_fifteen_of_sixteen(self):
        # The exact case observed live: one wedged export, fifteen good clips.
        assert derive_status(["SUCCEEDED"] * 15 + ["STALLED"]) == "partial"


class TestSignedUrlExpiry:
    def test_parses_goog_signature_deadline(self):
        url = (
            "https://storage.googleapis.com/bucket/footage.mp4"
            "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
            "&X-Goog-Date=20260831T195338Z&X-Goog-Expires=3600"
        )
        assert signed_url_expiry(url) == datetime(
            2026, 8, 31, 20, 53, 38, tzinfo=timezone.utc
        )

    def test_returns_none_for_unsigned_url(self):
        assert signed_url_expiry("https://example.com/a.mp4") is None

    def test_returns_none_for_garbage(self):
        assert signed_url_expiry("not a url at all") is None

    def test_returns_none_for_empty(self):
        assert signed_url_expiry("") is None


class TestPlanWindows:
    def setup_method(self):
        self.site = SiteMap(
            location_id=1,
            location_name="Test",
            timezone="America/Chicago",
            transit_seconds=240,
            clip_seconds=120,
            pad_before_seconds=30,
            pad_after_seconds=60,
            cameras=[
                Camera(id=1, name="LPR", role="entry"),
                Camera(id=2, name="Mid", role="tunnel"),
                Camera(id=3, name="Exit", role="exit"),
            ],
        )
        self.t0 = datetime(2026, 8, 30, 15, 9, 0, tzinfo=timezone.utc)

    def test_one_entry_per_camera(self):
        assert len(plan_windows(self.site, self.t0)) == 3

    def test_entry_window_brackets_t0(self):
        entry = plan_windows(self.site, self.t0)[0]
        assert entry.window_start == "2026-08-30T15:08:30.000Z"
        assert entry.window_end == "2026-08-30T15:12:00.000Z"

    def test_exit_window_excludes_t0(self):
        # The whole reason offsets exist: at a 240s transit the exit camera
        # must not be clipping the moment the car entered.
        exit_cam = plan_windows(self.site, self.t0)[-1]
        assert exit_cam.window_start == "2026-08-30T15:12:30.000Z"

    def test_ordered_by_offset(self):
        offs = [c.offset_seconds for c in plan_windows(self.site, self.t0)]
        assert offs == sorted(offs)

    def test_union_span_covers_everything(self):
        cams = plan_windows(self.site, self.t0)
        start, end = union_span(cams)
        assert start == cams[0].window_start
        assert end == cams[-1].window_end


class TestPlateHelpers:
    def test_normalize_strips_punctuation(self):
        assert normalize_plate(" abc-1234 ") == "ABC1234"

    def test_fuzzy_keeps_original_first(self):
        assert fuzzy_variants("8B00000")[0] == "8B00000"

    def test_fuzzy_generates_lookalikes(self):
        variants = fuzzy_variants("B0S")
        assert "80S" in variants and "BOS" in variants and "B05" in variants

    def test_fuzzy_has_no_duplicates(self):
        v = fuzzy_variants("OO00")
        assert len(v) == len(set(v))

    def test_fuzzy_on_plate_without_confusables(self):
        assert fuzzy_variants("XYW") == ["XYW"]

    def test_fuzzy_empty(self):
        assert fuzzy_variants("") == []


class TestSelectShareCameras:
    """Spot caps a shared view at 16 cameras. A real site can exceed that:
    up to 15 on inspection arches plus 8 in the tunnel."""

    @staticmethod
    def cams(n_entry, n_tunnel, n_exit):
        from spotai.claims import ClaimCamera
        out, cid = [], 1
        for role, count in (("entry", n_entry), ("tunnel", n_tunnel),
                            ("exit", n_exit)):
            for _ in range(count):
                out.append(ClaimCamera(cid, f"{role}-{cid}", role, cid, "", ""))
                cid += 1
        return out

    def test_under_the_cap_keeps_everything(self):
        cams = self.cams(2, 4, 2)
        assert select_share_cameras(cams) == [c.camera_id for c in cams]

    def test_exactly_at_the_cap_keeps_everything(self):
        cams = self.cams(4, 8, 4)
        assert len(select_share_cameras(cams)) == 16

    def test_full_23_camera_site_fits_the_cap(self):
        assert len(select_share_cameras(self.cams(7, 8, 8))) == 16

    def test_exit_arch_is_never_dropped(self):
        # The exit arch is the footage that shows the damage. Taking the
        # first 16 in tunnel order would drop it entirely.
        cams = self.cams(7, 8, 8)
        chosen = set(select_share_cameras(cams))
        exits = [c.camera_id for c in cams if c.role == "exit"]
        assert all(e in chosen for e in exits)

    def test_entry_arch_is_never_dropped(self):
        cams = self.cams(7, 8, 8)
        chosen = set(select_share_cameras(cams))
        entries = [c.camera_id for c in cams if c.role == "entry"]
        assert all(e in chosen for e in entries)

    def test_tunnel_cameras_are_the_ones_thinned(self):
        cams = self.cams(7, 8, 8)
        chosen = set(select_share_cameras(cams))
        tunnel = [c.camera_id for c in cams if c.role == "tunnel"]
        kept = [t for t in tunnel if t in chosen]
        assert 0 < len(kept) < len(tunnel)

    def test_thinned_tunnel_cameras_are_spread_not_contiguous(self):
        # An even spread covers the tunnel; a contiguous run leaves a blind
        # stretch where the damage may have happened.
        cams = self.cams(1, 20, 1)   # 22 total, so the tunnel must be thinned
        chosen = select_share_cameras(cams)
        tunnel_ids = [c.camera_id for c in cams if c.role == "tunnel"]
        kept = [t for t in tunnel_ids if t in set(chosen)]
        assert kept[-1] - kept[0] > len(kept)

    def test_arches_alone_exceeding_the_cap_still_returns_16(self):
        assert len(select_share_cameras(self.cams(10, 0, 10))) == 16

    def test_arch_overflow_keeps_both_arches_represented(self):
        cams = self.cams(10, 0, 10)
        chosen = set(select_share_cameras(cams))
        assert any(c.camera_id in chosen for c in cams if c.role == "entry")
        assert any(c.camera_id in chosen for c in cams if c.role == "exit")

    def test_result_is_in_tunnel_order(self):
        chosen = select_share_cameras(self.cams(7, 8, 8))
        assert chosen == sorted(chosen)

    def test_no_duplicates(self):
        chosen = select_share_cameras(self.cams(7, 8, 8))
        assert len(chosen) == len(set(chosen))


class TestMultiDeviceClaims:
    """Spot caps a device at 4 cameras. A 23-camera claim needs 6 devices
    rather than losing 19 cameras."""

    def test_chunks_of_four(self):
        assert chunk_cameras(list(range(1, 24))) == [
            [1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12],
            [13, 14, 15, 16], [17, 18, 19, 20], [21, 22, 23],
        ]

    def test_twenty_three_cameras_need_six_devices(self):
        assert len(chunk_cameras(list(range(23)))) == 6

    def test_four_or_fewer_stays_one_device(self):
        assert len(chunk_cameras([1, 2, 3, 4])) == 1
        assert len(chunk_cameras([1])) == 1

    def test_no_chunk_exceeds_the_cap(self):
        assert all(len(c) <= 4 for c in chunk_cameras(list(range(23))))

    def test_every_camera_survives_chunking(self):
        cams = list(range(23))
        assert [c for group in chunk_cameras(cams) for c in group] == cams

    def test_empty_still_yields_one_group(self):
        assert chunk_cameras([]) == [[]]

    def test_rejects_bad_chunk_size(self):
        with pytest.raises(ValueError):
            chunk_cameras([1, 2], size=0)


class TestMultiDeviceNaming:
    def test_single_device_has_no_part_suffix(self):
        assert device_name_part("J.Smith", "2026-08-30", 1, 1) == "J.Smith | 2026-08-30"

    def test_multi_device_is_numbered(self):
        assert device_name_part("J.Smith", "2026-08-30", 2, 6) == (
            "J.Smith | 2026-08-30 (2/6)"
        )

    def test_parts_still_fit_the_forty_char_cap(self):
        for part in range(1, 7):
            name = device_name_part("Bartholomew Fotheringay", "2026-08-30", part, 6)
            assert len(name) <= MAX_DEVICE_NAME, name

    def test_part_marker_survives_truncation(self):
        name = device_name_part("A" * 60, "2026-08-30", 3, 6)
        assert name.endswith("(3/6)")
        assert len(name) <= MAX_DEVICE_NAME

    def test_parts_sort_together(self):
        names = [device_name_part("J.Smith", "2026-08-30", p, 6) for p in range(1, 7)]
        assert names == sorted(names)


class TestPartExternalIds:
    def test_first_part_keeps_the_base_id(self):
        # Idempotency looks up the base, so part 1 must not be suffixed.
        assert part_external_id("WHEATON:C1:ABC:2026-08-30", 1) == (
            "WHEATON:C1:ABC:2026-08-30"
        )

    def test_later_parts_are_suffixed(self):
        assert part_external_id("WHEATON:C1:ABC:2026-08-30", 3) == (
            "WHEATON:C1:ABC:2026-08-30#P3"
        )

    def test_all_parts_are_unique(self):
        base = "WHEATON:C1:ABC:2026-08-30"
        ids = [part_external_id(base, p) for p in range(1, 7)]
        assert len(set(ids)) == 6

    def test_respects_the_length_cap(self):
        assert len(part_external_id("X" * 255, 6)) <= 255

    def test_suffix_survives_truncation(self):
        assert part_external_id("X" * 255, 6).endswith("#P6")
