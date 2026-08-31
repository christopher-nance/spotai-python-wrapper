"""Unit tests for the time-window math."""

from datetime import datetime, timezone

import pytest

from spotai import timewin as tw

TZ = "America/Chicago"


class TestParseLocal:
    def test_parses_wall_clock_to_utc_during_cdt(self):
        # Chicago is UTC-5 in August (CDT)
        got = tw.parse_local("2026-08-31 14:32", TZ)
        assert got == datetime(2026, 8, 31, 19, 32, tzinfo=timezone.utc)

    def test_parses_wall_clock_to_utc_during_cst(self):
        # Chicago is UTC-6 in January (CST) - the DST boundary matters
        got = tw.parse_local("2026-01-15 14:32", TZ)
        assert got == datetime(2026, 1, 15, 20, 32, tzinfo=timezone.utc)

    def test_accepts_seconds(self):
        got = tw.parse_local("2026-08-31 14:32:10", TZ)
        assert got == datetime(2026, 8, 31, 19, 32, 10, tzinfo=timezone.utc)

    def test_accepts_iso_t_separator(self):
        assert tw.parse_local("2026-08-31T14:32", TZ) == tw.parse_local(
            "2026-08-31 14:32", TZ
        )

    def test_accepts_us_slash_format(self):
        assert tw.parse_local("08/31/2026 14:32", TZ) == tw.parse_local(
            "2026-08-31 14:32", TZ
        )

    def test_bare_date_is_local_midnight(self):
        got = tw.parse_local("2026-08-31", TZ)
        assert got == datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)

    def test_explicit_z_ignores_local_timezone(self):
        got = tw.parse_local("2026-08-31T14:32:00Z", TZ)
        assert got == datetime(2026, 8, 31, 14, 32, tzinfo=timezone.utc)

    def test_explicit_offset_ignores_local_timezone(self):
        got = tw.parse_local("2026-08-31T14:32:00-04:00", TZ)
        assert got == datetime(2026, 8, 31, 18, 32, tzinfo=timezone.utc)

    def test_garbage_raises(self):
        with pytest.raises(tw.TimeParseError):
            tw.parse_local("last tuesday", TZ)

    def test_empty_raises(self):
        with pytest.raises(tw.TimeParseError):
            tw.parse_local("", TZ)

    def test_unknown_timezone_raises(self):
        with pytest.raises(tw.TimeParseError):
            tw.parse_local("2026-08-31 14:32", "Mars/Olympus_Mons")


class TestParseApiTs:
    def test_parses_z_form(self):
        assert tw.parse_api_ts("2026-08-31T19:32:10.500Z") == datetime(
            2026, 8, 31, 19, 32, 10, 500000, tzinfo=timezone.utc
        )

    def test_naive_treated_as_utc(self):
        assert tw.parse_api_ts("2026-08-31T19:32:10") == datetime(
            2026, 8, 31, 19, 32, 10, tzinfo=timezone.utc
        )


class TestIsoZ:
    def test_round_trips_through_api_format(self):
        dt = datetime(2026, 8, 31, 19, 32, 10, tzinfo=timezone.utc)
        assert tw.iso_z(dt) == "2026-08-31T19:32:10.000Z"

    def test_converts_non_utc_input(self):
        local = tw.parse_local("2026-08-31 14:32", TZ)
        assert tw.iso_z(local) == "2026-08-31T19:32:00.000Z"


class TestSeedOffsets:
    def test_distributes_between_entry_and_exit(self):
        # 5 tunnel cams across a 240s transit sit at 40/80/120/160/200
        assert tw.seed_offsets(5, 240) == [40, 80, 120, 160, 200]

    def test_never_lands_on_entry_or_exit(self):
        offs = tw.seed_offsets(5, 240)
        assert all(0 < o < 240 for o in offs)

    def test_single_camera_sits_midway(self):
        assert tw.seed_offsets(1, 240) == [120]

    def test_zero_cameras(self):
        assert tw.seed_offsets(0, 240) == []

    def test_offsets_are_ascending(self):
        offs = tw.seed_offsets(7, 210)
        assert offs == sorted(offs)


class TestWindowFor:
    def setup_method(self):
        self.t0 = datetime(2026, 8, 31, 19, 32, 0, tzinfo=timezone.utc)

    def test_entry_camera_window(self):
        start, end = tw.window_for(self.t0, 0, 120, 30, 60)
        assert start == datetime(2026, 8, 31, 19, 31, 30, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 31, 19, 35, 0, tzinfo=timezone.utc)

    def test_offset_shifts_whole_window(self):
        start, end = tw.window_for(self.t0, 240, 120, 30, 60)
        assert start == datetime(2026, 8, 31, 19, 35, 30, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 31, 19, 39, 0, tzinfo=timezone.utc)

    def test_exit_camera_window_excludes_t0(self):
        # The whole point of offsets: at 4 min transit the exit cam window
        # must not contain T0, or we clipped the wrong car.
        start, _ = tw.window_for(self.t0, 240, 120, 30, 60)
        assert start > self.t0

    def test_rejects_nonpositive_clip(self):
        with pytest.raises(ValueError):
            tw.window_for(self.t0, 0, 0, 30, 60)

    def test_rejects_negative_padding(self):
        with pytest.raises(ValueError):
            tw.window_for(self.t0, 0, 120, -1, 60)


class TestUnionWindow:
    def test_spans_all_windows(self):
        t0 = datetime(2026, 8, 31, 19, 32, 0, tzinfo=timezone.utc)
        wins = [tw.window_for(t0, o, 120, 30, 60) for o in (0, 120, 240)]
        start, end = tw.union_window(wins)
        assert start == wins[0][0]
        assert end == wins[-1][1]

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            tw.union_window([])


class TestDayBounds:
    def test_covers_exactly_24_hours_on_normal_day(self):
        start, end = tw.day_bounds_utc("2026-08-31", TZ)
        assert (end - start).total_seconds() == 86400
        assert start == datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
