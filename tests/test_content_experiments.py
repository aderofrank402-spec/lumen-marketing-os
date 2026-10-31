from __future__ import annotations

import copy

import pytest

from lumen_marketing_os.workflows.content.experiments import analyze_variants


def test_clear_separation_proposes_candidate_without_mutating_inputs():
    rows = [
        {"variant_key": "B", "exposures": 500, "conversions": 15},
        {"variant_key": "A", "exposures": 500, "conversions": 100},
    ]
    original = copy.deepcopy(rows)
    result = analyze_variants(rows, minimum_exposures=100, minimum_conversions=20)
    assert result["status"] == "recommendation"
    assert result["recommended_variant"] == "A"
    assert result["reasons"] == []
    assert result["requires_human_approval"] is True
    assert rows == original
    assert [item["variant_key"] for item in result["variants"]] == ["A", "B"]
    assert result["variants"][0]["wilson_lower"] > result["variants"][1]["wilson_upper"]


def test_low_sample_and_low_conversions_never_recommend():
    result = analyze_variants([
        {"variant_key": "A", "exposures": 5, "conversions": 4},
        {"variant_key": "B", "exposures": 5, "conversions": 0},
    ], minimum_exposures=100, minimum_conversions=10)
    assert result["status"] == "insufficient_evidence"
    assert result["recommended_variant"] is None
    assert result["reasons"] == ["minimum_exposures_not_met", "minimum_conversions_not_met"]


def test_overlapping_intervals_and_ties_do_not_recommend():
    for conversions in ((20, 17), (20, 20)):
        result = analyze_variants([
            {"variant_key": "A", "exposures": 100, "conversions": conversions[0]},
            {"variant_key": "B", "exposures": 100, "conversions": conversions[1]},
        ], minimum_exposures=50, minimum_conversions=10)
        assert result["status"] == "insufficient_evidence"
        assert result["reasons"] == ["wilson_intervals_overlap"]


def test_no_variants_and_zero_exposures_are_safe():
    empty = analyze_variants([], minimum_exposures=10, minimum_conversions=1)
    assert empty["status"] == "insufficient_evidence"
    zero = analyze_variants([
        {"variant_key": "A", "exposures": 0, "conversions": 0},
        {"variant_key": "B", "exposures": 10, "conversions": 1},
    ], minimum_exposures=10, minimum_conversions=1)
    assert zero["status"] == "insufficient_evidence"
    assert zero["variants"][0]["conversion_rate"] is None
    assert zero["variants"][0]["wilson_lower"] == 0.0
    assert zero["variants"][0]["wilson_upper"] == 1.0


def test_third_uncertain_arm_blocks_optimistic_runner_up_comparison():
    result = analyze_variants([
        {"variant_key": "A", "exposures": 1000, "conversions": 170},
        {"variant_key": "B", "exposures": 1000, "conversions": 100},
        {"variant_key": "C", "exposures": 100, "conversions": 9},
    ], minimum_exposures=100, minimum_conversions=20)
    # B's interval is below A's, but C's smaller denominator makes it wider.
    assert result["status"] == "insufficient_evidence"
    assert result["reasons"] == ["wilson_intervals_overlap"]


@pytest.mark.parametrize("rows", [
    [{"variant_key": "A", "exposures": 1, "conversions": 2}],
    [{"variant_key": "A", "exposures": -1, "conversions": 0}],
    [{"variant_key": "A", "exposures": True, "conversions": 0}],
    [{"variant_key": "A", "exposures": 1.5, "conversions": 1}],
    [{"variant_key": "A", "exposures": 1, "conversions": -1}],
    [{"variant_key": " A", "exposures": 1, "conversions": 0}],
    [{"variant_key": "A", "exposures": 1, "conversions": 0},
     {"variant_key": "A", "exposures": 1, "conversions": 0}],
])
def test_invalid_variant_rows_are_rejected(rows):
    with pytest.raises(ValueError):
        analyze_variants(rows, minimum_exposures=1, minimum_conversions=1)


@pytest.mark.parametrize("kwargs", [
    {"minimum_exposures": 0, "minimum_conversions": 1},
    {"minimum_exposures": True, "minimum_conversions": 1},
    {"minimum_exposures": 1, "minimum_conversions": 0},
    {"minimum_exposures": 1, "minimum_conversions": 1, "confidence_z": float("nan")},
    {"minimum_exposures": 1, "minimum_conversions": 1, "confidence_z": 1e308},
    {"minimum_exposures": 1, "minimum_conversions": 1, "confidence_z": 0},
])
def test_invalid_analysis_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        analyze_variants([], **kwargs)
