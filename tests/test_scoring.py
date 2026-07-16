"""Scoring is the one thing in this build that must not drift.

The hero-member numbers below are the ones spoken on stage and printed in the
deck. If these tests fail, the demo script is wrong.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from caregap_compass import bq, config, scoring, weights

HERO = "MBR00030"
AS_OF = _dt.date(2026, 6, 10)


@pytest.fixture(scope="module")
def hero_ranking():
    return scoring.rank_gaps(
        HERO,
        bq.get_open_gaps(HERO),
        bq.get_dispositions_for_member(HERO),
        bq.get_interventions(),
        as_of=AS_OF,
    )


# --- the demo -------------------------------------------------------------


def test_hero_selects_cbp(hero_ranking):
    assert hero_ranking["selected"]["measure_id"] == "CBP"
    assert hero_ranking["selected"]["score"] == pytest.approx(0.655, abs=0.01)


def test_hero_margin_is_decisive(hero_ranking):
    """The rejection has to land visibly, not by a hair."""
    assert hero_ranking["margin_over_runner_up"] >= 2.0


def test_hero_cbp_is_forty_days_out(hero_ranking):
    assert hero_ranking["selected"]["components"]["days_to_close"] == 40
    assert hero_ranking["selected"]["components"]["urgency"] == pytest.approx(0.85, abs=0.01)


def test_hero_rejects_both_others_with_a_reason(hero_ranking):
    rejected = {r["measure_id"]: r for r in hero_ranking["rejected"]}
    assert set(rejected) == {"COA", "OMW"}
    for entry in rejected.values():
        assert entry["rejected_because"].strip()


def test_cbp_wins_on_weight_not_propensity(hero_ranking):
    """The proposal's whole argument. COA is the easier close; CBP is worth 3x
    more. If this ever inverts, the pitch changes."""
    coa = next(r for r in hero_ranking["rejected"] if r["measure_id"] == "COA")
    assert coa["components"]["propensity"] > hero_ranking["selected"]["components"]["propensity"]
    assert coa["components"]["weight"] < hero_ranking["selected"]["components"]["weight"]


def test_decomposition_shows_the_arithmetic(hero_ranking):
    text = hero_ranking["decomposition_text"]
    assert "<- SELECTED" in text
    assert text.count("\n") == 2
    for token in ("weight 3.0", "urgency 0.85", "CBP", "COA", "OMW"):
        assert token in text


def test_recommended_channel_is_a_channel_the_member_answers(hero_ranking):
    assert hero_ranking["selected"]["components"]["recommended_channel"] == "Call Center"


# --- components -----------------------------------------------------------


def test_urgency_curve():
    assert scoring.urgency(40) == pytest.approx(0.85, abs=0.01)
    assert scoring.urgency(0) == 1.0
    assert scoring.urgency(-30) == 1.0  # overdue is urgent, not negative
    assert scoring.urgency(10_000) == 0.05  # clamped, never zero
    assert scoring.urgency(270) == pytest.approx(0.05, abs=0.01)


def test_disposition_classification_handles_both_encodings():
    assert scoring.classify_disposition("REPLIED_YES") == 1
    assert scoring.classify_disposition("Member confirmed appointment scheduled") == 1
    assert scoring.classify_disposition("RETURNED_UNDELIVERABLE") == -1
    assert scoring.classify_disposition("Wrong number") == -1
    assert scoring.classify_disposition("Member declined — not interested") == -1  # em dash
    assert scoring.classify_disposition("SOMETHING_NEW") == 0


def test_delivered_is_not_a_response():
    """A carrier receipt says the envelope arrived, not that the member acted.
    Counting it as engagement makes a dead channel look alive."""
    assert scoring.classify_disposition("DELIVERED") == 0
    rates = scoring.member_channel_rates(
        [
            {"channel": "Mail", "raw_disposition_code": "DELIVERED"},
            {"channel": "Mail", "raw_disposition_code": "RETURNED_UNDELIVERABLE"},
        ]
    )
    assert rates["Mail"]["attempts"] == 2
    assert rates["Mail"]["response_rate"] == 0.0


def test_channel_normalization():
    assert scoring.normalize_channel("Web Form") == "Web"
    assert scoring.split_channels("Provider+Web") == ["Provider", "Web"]
    assert scoring.split_channels("Mail+Pharmacy") == ["Mail", "Pharmacy"]


def test_member_channel_rates_count_unknowns_as_attempts():
    rates = scoring.member_channel_rates(
        [
            {"channel": "SMS", "raw_disposition_code": "REPLIED_YES"},
            {"channel": "SMS", "raw_disposition_code": "INVALID_NUM"},
            {"channel": "SMS", "raw_disposition_code": "MYSTERY_CODE"},
        ]
    )
    assert rates["SMS"]["attempts"] == 3
    assert rates["SMS"]["positive"] == 1
    assert rates["SMS"]["response_rate"] == pytest.approx(1 / 3, abs=0.01)


def test_hero_mail_is_the_dead_channel():
    """Mailed twice, never once acted -- one delivered, one returned
    undeliverable. That fact carries the rejection line on stage."""
    rates = scoring.member_channel_rates(bq.get_dispositions_for_member(HERO))
    assert rates["Mail"]["attempts"] == 2
    assert rates["Mail"]["response_rate"] == 0.0
    assert rates["Call Center"]["response_rate"] == pytest.approx(0.75, abs=0.01)


# --- weights --------------------------------------------------------------


def test_cms_weight_classes():
    assert weights.weight_for("CBP") == 3.0
    assert weights.weight_for("CDC-H") == 3.0
    assert weights.weight_for("COL") == 1.0
    assert weights.weight_for("AWC") == 0.5  # not scored, but still rankable
    assert weights.plain_weight("CBP") == "triple-weighted"
    assert weights.plain_weight("COA") == "single-weighted"


def test_weight_table_agrees_with_the_dataset():
    """If stars_performance and the CMS class table ever disagree, we want a
    loud failure here rather than a quiet wrong number on stage."""
    for row in bq.get_stars():
        info = weights.describe(row["measure_id"], row)
        assert "weight_conflict" not in info, info.get("weight_conflict")


# --- fallback ladder ------------------------------------------------------


def test_measure_only_mode_when_member_history_is_thin():
    ranking = scoring.rank_gaps(
        HERO, bq.get_open_gaps(HERO), [], bq.get_interventions(), as_of=AS_OF
    )
    assert ranking["scoring_mode"] == scoring.MODE_MEASURE_ONLY
    assert ranking["selected"]["measure_id"] == "CBP"  # rejection survives


def test_degraded_mode_when_no_intervention_data():
    ranking = scoring.rank_gaps(HERO, bq.get_open_gaps(HERO), [], [], as_of=AS_OF)
    assert ranking["scoring_mode"] == scoring.MODE_DEGRADED
    assert ranking["selected"]["measure_id"] == "CBP"  # weight alone still wins
    assert "gap age" in ranking["decomposition_text"]
    assert "gap_age" in ranking["selected"]["components"]


def test_no_open_gaps_is_not_an_error():
    ranking = scoring.rank_gaps("MBR99999", [], [], [], as_of=AS_OF)
    assert ranking["status"] == "no_open_gaps"
    assert ranking["selected"] is None


def test_ranking_is_stable_across_runs():
    """Same inputs, same order. A demo that reshuffles on refresh isn't auditable."""
    runs = [
        scoring.rank_gaps(
            HERO,
            bq.get_open_gaps(HERO),
            bq.get_dispositions_for_member(HERO),
            bq.get_interventions(),
            as_of=AS_OF,
        )["decomposition_text"]
        for _ in range(3)
    ]
    assert len(set(runs)) == 1


# --- clock ----------------------------------------------------------------


def test_demo_clock_is_pinned():
    """Every slot in appointment_slots predates the real wall clock. If this
    unpins, days-to-close goes negative and booking finds nothing."""
    assert config.today() == AS_OF
