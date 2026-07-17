"""Actioner: explain, find, book — plus the claims evidence and the guardrails.

The theme: anything with a consequence must be re-checked at the moment it
happens, and must never be claimed without a receipt.
"""

from __future__ import annotations

import pytest

from caregap_compass import actioner, bq, config, measures, prioritizer

HERO = config.HERO_MEMBER_ID


class Ctx:
    def __init__(self, **state):
        self.state = dict(state)


@pytest.fixture(autouse=True)
def isolate_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_FILE", tmp_path / "audit.jsonl")
    actioner.BOOKINGS.clear()
    actioner.CALLBACKS.clear()
    actioner.DISPUTES.clear()


@pytest.fixture
def ctx():
    context = Ctx()
    prioritizer.rank_open_gaps(HERO, context)  # selects CBP, seeds session state
    return context


@pytest.fixture
def gap_id(ctx):
    return ctx.state["selected_gap_id"]


# --- explain_gap -----------------------------------------------------------


def test_explain_states_a_rule_not_a_decision(ctx, gap_id):
    result = actioner.explain_gap(gap_id, HERO, ctx)
    assert result["status"] == "ok"
    assert result["cost"]["is_determination"] is False
    assert result["cost"]["disclaimer"]


def test_explain_is_grounded_in_transcripts(ctx, gap_id):
    result = actioner.explain_gap(gap_id, HERO, ctx)
    assert result["member_voices"]
    for voice in result["member_voices"]:
        assert voice["source"].endswith(".txt")  # every claim is attributable


def test_explain_stays_member_facing(ctx, gap_id):
    result = actioner.explain_gap(gap_id, HERO, ctx)
    why = result["why_it_matters"]
    assert "due" in why["for_you"]
    assert why["next_step"] == measures.action_for(result["measure_id"])
    assert "weight" not in why
    assert "for_the_plan" not in why


def test_explain_returns_trusted_sources(ctx, gap_id):
    result = actioner.explain_gap(gap_id, HERO, ctx)
    assert result["trusted_sources"]
    for source in result["trusted_sources"]:
        assert source["title"]
        assert source["url"].startswith("https://")


def test_explain_refuses_another_members_gap(ctx):
    other = next(g for g in bq.get_all_gaps() if g["member_id"] != HERO)
    result = actioner.explain_gap(other["gap_id"], HERO, ctx)
    assert result["error_code"] == "GAP_NOT_FOUND"


# --- claims evidence -------------------------------------------------------


def test_paid_claim_surfaces_as_already_done():
    """34 open gaps have a paid claim on the measure's own CPT. Telling those
    members to go book the appointment they already had is precisely the noise
    this product exists to stop."""
    hit = None
    claims_by_member: dict[str, list] = {}
    for claim in bq.fetch_table("claims"):
        claims_by_member.setdefault(claim["member_id"], []).append(claim)
    for gap in bq.get_all_gaps():
        if gap["gap_status"] != "Open":
            continue
        cpts = set(measures.cpt_codes_for(gap["measure_id"]))
        if not cpts:
            continue
        for claim in claims_by_member.get(gap["member_id"], []):
            if claim["cpt_code"] in cpts and claim["claim_status"] == "Paid":
                hit = (gap, claim)
                break
        if hit:
            break
    assert hit, "fixture expectation: some open gaps have a paid claim"

    gap, _ = hit
    context = Ctx(authenticated_member_id=gap["member_id"])
    result = actioner.explain_gap(gap["gap_id"], gap["member_id"], context)
    assert result["claim_history"]["claims_checked"] is True
    assert result["claim_history"]["already_paid"]
    assert "crediting lag" in result["claim_history"]["note"]


def test_claims_lookup_is_scoped_to_the_measures_cpts(ctx):
    evidence = actioner._claims_for_gap(HERO, "CBP")
    assert evidence["checked"] is True
    assert set(evidence["cpt_codes_checked"]) == set(measures.cpt_codes_for("CBP"))


def test_measure_without_cpt_mapping_is_not_checked():
    evidence = actioner._claims_for_gap(HERO, "SPC")
    assert evidence["checked"] is False


# --- find_provider ---------------------------------------------------------


def test_provider_search_returns_distinct_in_network_options(ctx, gap_id):
    result = actioner.find_provider(gap_id, HERO, ctx)
    assert result["status"] == "ok"
    recs = result["recommendations"]
    assert 0 < len(recs) <= actioner.MAX_CHOICES
    ids = [r["provider_id"] for r in recs]
    assert len(set(ids)) == len(ids), "three slots with one doctor is one choice"
    assert all(r["network_status"] == "In-Network" for r in recs)


def test_provider_search_never_offers_a_past_slot(ctx, gap_id):
    result = actioner.find_provider(gap_id, HERO, ctx)
    for rec in result["recommendations"]:
        assert str(rec["slot_date"]) >= config.today().isoformat()


def test_provider_scores_are_bounded(ctx, gap_id):
    """The original absolute formula produced -346 on this dataset's synthetic
    geography. Scores are relative now and must stay presentable."""
    for rec in actioner.find_provider(gap_id, HERO, ctx)["recommendations"]:
        assert 0.0 <= rec["score"] <= 100.0


def test_distance_is_flagged_synthetic_and_ranked_not_quoted(ctx, gap_id):
    """The hero has a Maine address, a California zip and Oregon coordinates.
    Mileage is meaningless, so reasons must talk in ranks, not miles."""
    result = actioner.find_provider(gap_id, HERO, ctx)
    assert "synthetic" in result["geography_note"].lower()
    for rec in result["recommendations"]:
        assert rec["distance_is_synthetic"] is True
        assert rec["distance_rank"] >= 1
        assert not any("miles away" in reason for reason in rec["ranking_reasons"])


def test_only_slots_that_can_close_the_gap_are_offered(ctx, gap_id):
    allowed = set(measures.visit_types_for("CBP"))
    for rec in actioner.find_provider(gap_id, HERO, ctx)["recommendations"]:
        assert rec["visit_type"] in allowed


# --- booking ---------------------------------------------------------------


@pytest.fixture
def top_pick(ctx, gap_id):
    return actioner.find_provider(gap_id, HERO, ctx)["recommendations"][0]


def test_booking_returns_a_receipt(ctx, gap_id, top_pick):
    receipt = actioner.book_or_callback(
        "book", gap_id, HERO, top_pick["provider_id"], top_pick["slot_id"], ctx
    )
    assert receipt["booking_status"] == "booked"
    assert receipt["confirmation_id"].startswith("APPT-")
    assert receipt["simulated"] is True


def test_booking_is_idempotent(ctx, gap_id, top_pick):
    """A retry must not create a second appointment for a real person."""
    args = ("book", gap_id, HERO, top_pick["provider_id"], top_pick["slot_id"], ctx)
    first = actioner.book_or_callback(*args)
    second = actioner.book_or_callback(*args)
    assert second["idempotent_replay"] is True
    assert second["confirmation_id"] == first["confirmation_id"]
    assert len(actioner.BOOKINGS) == 1


def test_booking_revalidates_visit_type(ctx, gap_id):
    """Search results go stale. A colonoscopy slot cannot close a blood-pressure
    gap no matter what the search said three turns ago."""
    wrong = next(s for s in bq.get_slots() if s["visit_type"] == "Colonoscopy" and s["available"])
    result = actioner.book_or_callback(
        "book", gap_id, HERO, wrong["provider_id"], wrong["slot_id"], ctx
    )
    assert result["error_code"] == "WRONG_VISIT_TYPE"
    assert not actioner.BOOKINGS


def test_booking_rejects_a_mismatched_provider(ctx, gap_id, top_pick):
    other = next(
        p for p in bq.get_providers() if p["provider_id"] != top_pick["provider_id"]
    )
    result = actioner.book_or_callback(
        "book", gap_id, HERO, other["provider_id"], top_pick["slot_id"], ctx
    )
    assert result["error_code"] == "SLOT_PROVIDER_MISMATCH"


def test_booking_rejects_an_unavailable_slot(ctx, gap_id):
    taken = next(s for s in bq.get_slots() if not s["available"])
    result = actioner.book_or_callback(
        "book", gap_id, HERO, taken["provider_id"], taken["slot_id"], ctx
    )
    assert result["error_code"] in ("SLOT_UNAVAILABLE", "WRONG_VISIT_TYPE", "OUT_OF_NETWORK")


def test_booking_requires_a_slot(ctx, gap_id):
    assert (
        actioner.book_or_callback("book", gap_id, HERO, "", "", ctx)["error_code"]
        == "MISSING_SLOT"
    )


def test_invalid_action_is_rejected(ctx, gap_id):
    assert (
        actioner.book_or_callback("teleport", gap_id, HERO, "", "", ctx)["error_code"]
        == "INVALID_ACTION"
    )


def test_callback_returns_a_receipt(ctx, gap_id):
    receipt = actioner.book_or_callback("callback", gap_id, HERO, "", "", ctx)
    assert receipt["callback_status"] == "callback_requested"
    assert receipt["callback_tracking_id"].startswith("CB-")


# --- dispute ---------------------------------------------------------------


def test_dispute_never_closes_the_gap(ctx, gap_id):
    """The member is believed; the record is not silently altered. Both."""
    receipt = actioner.submit_gap_dispute(gap_id, HERO, "Peterland Clinic", "March", ctx)
    assert receipt["review_status"] == "submitted_for_review"
    assert receipt["underlying_gap_changed"] is False
    assert bq.get_gap(gap_id)["gap_status"] == "Open"


def test_dispute_attaches_claim_evidence(ctx, gap_id):
    receipt = actioner.submit_gap_dispute(gap_id, HERO, "Peterland Clinic", "March", ctx)
    assert receipt["supporting_claims"]["checked"] is True


def test_dispute_needs_facility_and_date(ctx, gap_id):
    assert (
        actioner.submit_gap_dispute(gap_id, HERO, "", "March", ctx)["error_code"]
        == "FACILITY_REQUIRED"
    )
    assert (
        actioner.submit_gap_dispute(gap_id, HERO, "Clinic", "", ctx)["error_code"]
        == "APPROXIMATE_DATE_REQUIRED"
    )


def test_dispute_is_idempotent(ctx, gap_id):
    args = (gap_id, HERO, "Peterland Clinic", "March", ctx)
    first = actioner.submit_gap_dispute(*args)
    second = actioner.submit_gap_dispute(*args)
    assert second["idempotent_replay"] is True
    assert second["tracking_id"] == first["tracking_id"]


# --- authorization ---------------------------------------------------------


def test_session_cannot_pivot_to_another_member(ctx):
    result = actioner.find_provider("", "MBR00001", ctx)
    assert result["error_code"] == "MEMBER_SESSION_MISMATCH"
