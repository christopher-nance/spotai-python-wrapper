"""Tests for plate matching.

The scenarios here are taken from measured behaviour, not imagination:
521 live LPR reads from one site, and 796 real plate entries from a claims
export. Where a number appears in a test name it came from that data.
"""

from datetime import datetime, timezone

import pytest

from spotai.matching import (
    LIKELY,
    NEAR_CERTAIN,
    PlateCandidate,
    confidence_band,
    is_ambiguous,
    is_usable,
    normalize,
    rank_candidates,
    similarity,
    weighted_distance,
)


class Sighting:
    """Stand-in for lpr.PlateSighting."""

    def __init__(self, plate, visits=1):
        self.plate = plate
        self.visits = visits
        self.first_seen = datetime(2026, 8, 30, 15, 9, tzinfo=timezone.utc)
        self.last_seen = datetime(2026, 8, 30, 15, 10, tzinfo=timezone.utc)


class TestNormalize:
    def test_upper_cases(self):
        assert normalize("ab12345") == "AB12345"

    def test_strips_internal_space(self):
        # 12.4% of real entries contain one, e.g. "C12 3456"
        assert normalize("C12 3456") == "C123456"

    def test_strips_dashes_and_punctuation(self):
        assert normalize("AB-12345") == "AB12345"
        assert normalize("AB.123*45") == "AB12345"

    def test_empty(self):
        assert normalize("") == ""
        assert normalize(None) == ""


class TestIsUsable:
    """21 entries in the real export were placeholders or pasted notes."""

    @pytest.mark.parametrize("junk", ["N/A", "NA", "TEST", "NONE", "UNKNOWN",
                                      "1111", "XXXXX", "", "   "])
    def test_rejects_placeholders(self, junk):
        assert not is_usable(junk)

    def test_rejects_too_short(self):
        assert not is_usable("FM7")
        assert not is_usable("DC19")

    def test_rejects_pasted_notes(self):
        assert not is_usable("customer says damage on driver side rear quarter")

    def test_rejects_single_repeated_character(self):
        assert not is_usable("AAAAAAA")

    def test_accepts_real_plates(self):
        for p in ("AB12345", "C12 3456", "CD67890", "VANITY8", "9XYZ123"):
            assert is_usable(p), p


class TestWeightedDistance:
    def test_identical_is_zero(self):
        assert weighted_distance("AB12345", "AB12345") == 0.0

    def test_lookalike_substitution_is_cheap(self):
        assert weighted_distance("S0", "SO") == pytest.approx(0.30)

    def test_unrelated_substitution_is_full_cost(self):
        assert weighted_distance("SK", "SX") == pytest.approx(1.0)

    def test_lookalike_beats_unrelated(self):
        assert weighted_distance("B", "8") < weighted_distance("B", "K")


class TestSimilarity:
    TRUTH = "AB12345"

    def test_exact(self):
        assert similarity(self.TRUTH, self.TRUTH) == 1.0

    def test_case_and_punctuation_are_free(self):
        assert similarity("ab-12345", self.TRUTH) == 1.0

    def test_dropped_leading_letter_scores_high(self):
        # The dominant real failure: characters lost from the left.
        assert similarity("B12345", self.TRUTH) >= NEAR_CERTAIN

    def test_both_leading_letters_dropped_still_likely(self):
        assert similarity("12345", self.TRUTH) >= LIKELY

    def test_partial_tail_still_likely(self):
        assert similarity("2345", self.TRUTH) >= LIKELY

    def test_single_typo_is_likely(self):
        assert similarity("AB13345", self.TRUTH) >= LIKELY

    def test_state_prefix_is_tolerated(self):
        assert similarity("IL AB12345", self.TRUTH) >= LIKELY

    def test_unrelated_plate_scores_low(self):
        assert similarity("ZZ99999", self.TRUTH) < 0.55

    def test_suffix_beats_prefix(self):
        # Truncation happens on the left, so a matching tail means more.
        assert similarity("12345", self.TRUTH) > similarity("AB123", self.TRUTH)

    def test_empty_inputs(self):
        assert similarity("", self.TRUTH) == 0.0
        assert similarity(self.TRUTH, "") == 0.0


class TestConfidenceBand:
    def test_bands(self):
        assert confidence_band(1.00) == "near-certain"
        assert confidence_band(0.85) == "likely"
        assert confidence_band(0.70) == "possible"
        assert confidence_band(0.20) == "weak"

    def test_boundaries_are_inclusive(self):
        assert confidence_band(NEAR_CERTAIN) == "near-certain"
        assert confidence_band(LIKELY) == "likely"


class TestRankCandidates:
    POOL = [Sighting(p) for p in
            ["AB12345", "GH11223", "JK44556", "LM77889", "12345", "VANITY"]]

    def test_exact_match_ranks_first(self):
        out = rank_candidates("AB12345", self.POOL)
        assert out[0].plate == "AB12345"
        assert out[0].score == 1.0

    def test_truncated_read_is_found(self):
        out = rank_candidates("AB12345", self.POOL)
        assert "12345" in [c.plate for c in out]

    def test_sorted_descending(self):
        out = rank_candidates("AB12345", self.POOL)
        assert [c.score for c in out] == sorted([c.score for c in out], reverse=True)

    def test_unrelated_plate_returns_nothing(self):
        assert rank_candidates("QQ11111", self.POOL) == []

    def test_junk_entry_returns_nothing(self):
        # Must not fabricate a match from "N/A".
        assert rank_candidates("N/A", self.POOL) == []
        assert rank_candidates("TEST", self.POOL) == []

    def test_respects_limit(self):
        assert len(rank_candidates("AB12345", self.POOL, limit=2)) <= 2

    def test_empty_pool(self):
        assert rank_candidates("AB12345", []) == []

    def test_candidates_carry_band_and_visits(self):
        top = rank_candidates("AB12345", self.POOL)[0]
        assert top.band == "near-certain"
        assert top.visits == 1

    def test_auto_acceptable_reflects_threshold(self):
        top = rank_candidates("AB12345", self.POOL)[0]
        assert top.auto_acceptable

    def test_to_dict_is_json_safe(self):
        d = rank_candidates("AB12345", self.POOL)[0].to_dict()
        assert set(d) == {"plate", "score", "band", "visits"}


class TestAmbiguity:
    def test_clear_winner_is_not_ambiguous(self):
        cands = [
            PlateCandidate("A", 0.98, "near-certain", 1, None, None),
            PlateCandidate("B", 0.60, "possible", 1, None, None),
        ]
        assert not is_ambiguous(cands)

    def test_close_pair_is_ambiguous(self):
        cands = [
            PlateCandidate("A", 0.83, "likely", 1, None, None),
            PlateCandidate("B", 0.80, "likely", 1, None, None),
        ]
        assert is_ambiguous(cands)

    def test_single_candidate_is_not_ambiguous(self):
        assert not is_ambiguous(
            [PlateCandidate("A", 0.9, "likely", 1, None, None)]
        )

    def test_empty_is_not_ambiguous(self):
        assert not is_ambiguous([])


class TestRealWorldRegressions:
    """Cases drawn from the live comparison of portal entries to LPR reads."""

    def test_space_in_entry_matches_read(self):
        # Real: portal "C12 3456" vs LPR "C123456"
        assert similarity("C12 3456", "C123456") == 1.0

    def test_vanity_plate_matches_itself(self):
        assert similarity("VANITY8", "VANITY8") == 1.0

    def test_out_of_state_format_is_handled(self):
        # Tennessee sites produce DDDLLLL, not Illinois LLDDDDD.
        assert similarity("9XYZ123", "9XYZ123") == 1.0

    def test_car_that_was_never_read_returns_nothing(self):
        # Real: "QWE99Q" against that day's pool - best real score was 0.33.
        pool = [Sighting(p) for p in
                ["GH11223", "JK44556", "3344556", "VANITY", "LM77889"]]
        assert rank_candidates("QWE99Q", pool) == []
