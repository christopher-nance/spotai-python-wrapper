"""Tests for site map configuration, offset seeding, and site resolution."""

import pytest

from spotai import Camera, SiteMap
from spotai.errors import SiteMapNotFound
from spotai.sitemap import resolve_site_map


def make_site(**overrides):
    defaults = dict(
        location_id=6284,
        location_name="Example Wash: Wheaton",
        timezone="America/Chicago",
        transit_seconds=240,
        lpr_camera_id=1,
        cameras=[
            Camera(id=1, name="LPR", role="entry"),
            Camera(id=2, name="Tunnel Entrance", role="tunnel"),
            Camera(id=3, name="SmartStop 1", role="tunnel"),
            Camera(id=4, name="SmartStop 2", role="tunnel"),
            Camera(id=5, name="Exit Inspection", role="exit"),
            Camera(id=6, name="Pole Exit", role="exit"),
        ],
    )
    defaults.update(overrides)
    return SiteMap(**defaults)


class TestOffsetSeeding:
    def test_entry_is_zero(self):
        site = make_site()
        assert site.cameras[0].offset_seconds == 0

    def test_exits_sit_at_transit(self):
        site = make_site()
        exits = [c for c in site.cameras if c.role == "exit"]
        assert all(c.offset_seconds == 240 for c in exits)

    def test_tunnel_cameras_spread_between(self):
        site = make_site()
        tunnel = [c.offset_seconds for c in site.cameras if c.role == "tunnel"]
        assert tunnel == [60, 120, 180]

    def test_tunnel_offsets_never_touch_entry_or_exit(self):
        site = make_site()
        tunnel = [c.offset_seconds for c in site.cameras if c.role == "tunnel"]
        assert all(0 < o < 240 for o in tunnel)

    def test_explicit_offset_is_respected(self):
        site = make_site(
            cameras=[
                Camera(id=1, role="entry"),
                Camera(id=2, role="tunnel", offset_seconds=99),
            ]
        )
        assert site.cameras[1].offset_seconds == 99

    def test_ordered_by_offset(self):
        site = make_site()
        offs = [c.offset_seconds for c in site.ordered_cameras()]
        assert offs == sorted(offs)


class TestKeyCameras:
    def test_defaults_to_one_per_role_plus_last(self):
        site = make_site()
        # entry(1), first tunnel(2), first exit(5), last camera(6)
        assert site.key_camera_ids == [1, 2, 5, 6]

    def test_never_exceeds_spot_cap_of_four(self):
        site = make_site()
        assert len(site.key_camera_ids) <= 4

    def test_no_duplicates_when_roles_overlap(self):
        site = make_site(
            cameras=[Camera(id=1, role="entry"), Camera(id=2, role="exit")]
        )
        assert site.key_camera_ids == [1, 2]

    def test_explicit_selection_is_kept(self):
        site = make_site(key_camera_ids=[3, 4])
        assert site.key_camera_ids == [3, 4]

    def test_rejects_unknown_camera(self):
        with pytest.raises(ValueError, match="not in this site map"):
            make_site(key_camera_ids=[999])

    def test_allows_more_than_four_for_multi_device_claims(self):
        # Spot caps a *device* at 4 cameras, not a claim. More cameras means
        # more devices, which beats silently losing them.
        site = make_site(key_camera_ids=[1, 2, 3, 4, 5, 6])
        assert site.key_camera_ids == [1, 2, 3, 4, 5, 6]

    def test_all_camera_ids_can_be_used_wholesale(self):
        site = make_site(key_camera_ids=[])
        site2 = make_site(key_camera_ids=site.all_camera_ids())
        assert len(site2.key_camera_ids) == len(site2.cameras)


class TestValidation:
    def test_rejects_duplicate_camera_ids(self):
        with pytest.raises(ValueError, match="repeated"):
            make_site(
                cameras=[Camera(id=1, role="entry"), Camera(id=1, role="exit")]
            )

    def test_rejects_empty_camera_list(self):
        with pytest.raises(ValueError, match="at least one camera"):
            make_site(cameras=[])

    def test_rejects_bad_role(self):
        with pytest.raises(ValueError, match="role must be one of"):
            Camera(id=1, role="middle")

    def test_rejects_negative_offset(self):
        with pytest.raises(ValueError, match=">= 0"):
            Camera(id=1, offset_seconds=-5)

    def test_rejects_zero_clip_seconds(self):
        with pytest.raises(ValueError, match="clip_seconds"):
            make_site(clip_seconds=0)

    def test_camera_gets_default_name(self):
        assert Camera(id=42).name == "camera-42"


class TestSlug:
    def test_takes_part_after_colon(self):
        assert make_site().slug == "WHEATON"

    def test_handles_no_colon(self):
        assert make_site(location_name="Niles").slug == "NILES"

    def test_collapses_punctuation(self):
        assert make_site(location_name="Wash Co: Carol Stream").slug == "CAROL_STREAM"


class TestResolveSiteMap:
    def setup_method(self):
        self.sites = [
            make_site(),
            make_site(location_id=5810, location_name="Example Wash: Niles"),
            make_site(location_id=3989, location_name="Example Wash: Naperville"),
        ]

    def test_by_location_id(self):
        assert resolve_site_map(self.sites, 5810).location_name.endswith("Niles")

    def test_by_numeric_string(self):
        assert resolve_site_map(self.sites, "5810").location_id == 5810

    def test_by_exact_name(self):
        got = resolve_site_map(self.sites, "Example Wash: Niles")
        assert got.location_id == 5810

    def test_by_unique_substring(self):
        assert resolve_site_map(self.sites, "Wheaton").location_id == 6284

    def test_substring_is_case_insensitive(self):
        assert resolve_site_map(self.sites, "wheaton").location_id == 6284

    def test_ambiguous_substring_raises_rather_than_guessing(self):
        # Clipping the wrong building is worse than failing loudly.
        with pytest.raises(SiteMapNotFound, match="more than one site"):
            resolve_site_map(self.sites, "Example")

    def test_unknown_name_raises(self):
        with pytest.raises(SiteMapNotFound, match="No site map matching"):
            resolve_site_map(self.sites, "Atlantis")

    def test_unknown_id_raises(self):
        with pytest.raises(SiteMapNotFound, match="No site map with location_id"):
            resolve_site_map(self.sites, 1)

    def test_empty_site_maps_raises(self):
        with pytest.raises(SiteMapNotFound, match="No site maps configured"):
            resolve_site_map([], "Wheaton")


class TestRoundTrip:
    def test_to_dict_from_dict(self):
        site = make_site()
        clone = SiteMap.from_dict(site.to_dict())
        assert clone.to_dict() == site.to_dict()

    def test_from_dict_accepts_plain_camera_dicts(self):
        site = SiteMap.from_dict(
            {
                "location_id": 1,
                "location_name": "X",
                "cameras": [{"id": 1, "role": "entry"}, {"id": 2, "role": "exit"}],
            }
        )
        assert [c.id for c in site.cameras] == [1, 2]
