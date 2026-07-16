"""The impact number goes on a slide and gets said out loud to leadership.

These tests pin the arithmetic and, more importantly, pin the honesty: the
projection must never outrun a closure rate the plan has actually achieved.
"""

from __future__ import annotations

import pytest

from caregap_compass import impact


@pytest.fixture(scope="module")
def cbp():
    return impact.headline("CBP")


def test_cbp_headline_numbers(cbp):
    assert cbp["status"] == "ok"
    assert cbp["open_gap_count"] == 27
    assert cbp["members_eligible"] == 51
    assert cbp["members_compliant"] == 24
    assert cbp["current_rate_pct"] == pytest.approx(47.06, abs=0.01)
    assert cbp["current_star_rating"] == 1
    assert cbp["weight"] == 3.0


def test_derived_star_rating_matches_the_dataset(cbp):
    """We recompute the star rating from benchmarks rather than trusting the
    column. If the two ever disagree, one of them is wrong and we want to know."""
    assert cbp["current_star_rating"] == cbp["reported_star_rating"]


def test_star_ladder_is_the_published_one(cbp):
    ladder = {r["closes_needed"]: r["resulting_stars"] for r in cbp["star_ladder"]}
    assert ladder == {11: 2, 15: 3, 17: 4, 20: 5}


def test_projection_is_anchored_to_a_real_intervention(cbp):
    projection = cbp["projection"]
    assert projection["closure_rate_pct"] == pytest.approx(41.7, abs=0.01)
    assert projection["intervention_type"] == "PCP Panel Engagement"
    assert projection["expected_closes"] == 11


def test_projection_never_exceeds_historical_closure_rate(cbp):
    """The guard against shipping a vibe: whatever we claim, the implied closure
    rate must be one this plan has already achieved."""
    projection = cbp["projection"]
    implied_rate = projection["expected_closes"] / cbp["open_gap_count"] * 100
    assert implied_rate <= projection["closure_rate_pct"]


def test_ceiling_is_labelled_as_a_ceiling_not_claimed(cbp):
    """We may say '5 stars' out loud, but only as the size of the prize."""
    assert cbp["ceiling"]["resulting_stars"] == 5
    assert cbp["target"]["resulting_stars"] == 2
    assert cbp["target"]["closes_needed"] < cbp["ceiling"]["closes_needed"]
    assert "size of the prize" in cbp["sentence"]


def test_sentence_carries_the_load_bearing_facts(cbp):
    for token in ("27", "triple-weighted", "47.06%", "41.7%", "PCP Panel", "51"):
        assert token in cbp["sentence"], token


def test_best_historical_intervention_picks_the_best():
    intervention = impact.best_historical_intervention("CBP")
    assert intervention["closure_rate_pct"] == 41.7
    assert impact.best_historical_intervention("NOPE") is None


def test_unscored_measures_have_no_star_ladder():
    """AWC and FUH ship no benchmarks -- they are tracked, not scored. Inventing
    a star move for them would be fabrication."""
    for measure_id in ("AWC", "FUH"):
        result = impact.measure_impact(measure_id)
        assert result["star_ladder"] == []
        assert result["is_scored_star_measure"] is False


def test_cbp_is_the_top_weighted_opportunity():
    """Why CBP is the hero measure: weight x reachable star gain."""
    ranked = impact.rank_measures_by_opportunity()
    assert ranked[0]["measure_id"] in ("CBP", "CDC-H")
    assert ranked[0]["weighted_opportunity"] == 12.0
    unscored = {r["measure_id"]: r for r in ranked if r["measure_id"] in ("AWC", "FUH")}
    for row in unscored.values():
        assert row["weighted_opportunity"] == 0.0


def test_star_rating_boundaries():
    star_row = {
        "benchmark_2star_pct": 67.0,
        "benchmark_3star_pct": 75.0,
        "benchmark_4star_pct": 80.0,
        "benchmark_5star_pct": 86.0,
    }
    assert impact.star_rating_for(47.06, star_row) == 1
    assert impact.star_rating_for(66.99, star_row) == 1
    assert impact.star_rating_for(67.0, star_row) == 2  # exactly on the threshold
    assert impact.star_rating_for(86.0, star_row) == 5
    assert impact.star_rating_for(100.0, star_row) == 5
