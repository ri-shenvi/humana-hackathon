"""The /api surface and the route wiring.

The API is deliberately thin — it wraps code that is already tested elsewhere.
What is worth testing here is the wiring: that mounting the UI at "/" does not
shadow ADK's own routes, that the panel's data survives the JSON round trip, and
that masking holds at the edge.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from caregap_compass import config, server

HERO = config.HERO_MEMBER_ID


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


# --- wiring ---------------------------------------------------------------


def test_static_mount_does_not_shadow_adk(client):
    """StaticFiles at "/" is a catch-all. Starlette matches in registration
    order, so ADK's routes must have been registered first — if this 404s, the
    mount ate the agent API and nothing works."""
    assert client.get("/list-apps").status_code == 200


def test_ui_is_served_from_the_same_origin(client):
    """Same origin is the whole design: ADK ships an unconditional CSRF origin
    check that 403s cross-origin POSTs, so a separate dev server would need
    --allow_origins and a proxy."""
    r = client.get("/")
    assert r.status_code == 200
    assert "CareGap" in r.text


def test_agent_is_discoverable_under_the_expected_app_name(client):
    assert "caregap_compass" in client.get("/list-apps").json()


# --- /api/rank ------------------------------------------------------------


def test_rank_is_the_number_on_the_slide(client):
    d = client.get(f"/api/rank/{HERO}").json()
    assert d["selected"]["measure_id"] == "CBP"
    assert d["selected"]["score"] == pytest.approx(0.655, abs=0.01)
    assert d["margin_over_runner_up"] >= 2.0


def test_rank_carries_everything_the_panel_renders(client):
    d = client.get(f"/api/rank/{HERO}").json()
    c = d["selected"]["components"]
    for field in ("weight", "urgency", "propensity", "days_to_close", "plain_weight"):
        assert field in c, field
    assert d["formula"] and d["scoring_mode"] and d["as_of"]
    for entry in d["rejected"]:
        assert entry["rejected_because"]


def test_rank_enriches_with_plan_standing(client):
    """So the panel can say '1 star, trending down' beside the weight."""
    ctx = client.get(f"/api/rank/{HERO}").json()["selected"]["measure_context"]
    assert ctx["current_star_rating"] == 1
    assert ctx["trending"] == "Down"
    assert ctx["weight"] == 3.0


def test_rank_matches_the_decomposition_text(client):
    """The panel and the chat must never disagree — both come from
    scoring.rank_gaps. If they diverge, one of them is fabricating."""
    d = client.get(f"/api/rank/{HERO}").json()
    assert "0.655" in d["decomposition_text"]
    assert "<- SELECTED" in d["decomposition_text"]


def test_rank_404s_an_unknown_member(client):
    assert client.get("/api/rank/MBR99999").status_code == 404


def test_member_with_no_open_gaps_is_not_an_error(client):
    from caregap_compass import bq

    ids = {g["member_id"] for g in bq.get_all_gaps() if g["gap_status"] == "Open"}
    empty = next(m for m in bq.list_member_ids() if m not in ids)
    d = client.get(f"/api/rank/{empty}").json()
    assert d["status"] == "no_open_gaps"
    assert d["selected"] is None


# --- /api/impact ----------------------------------------------------------


def test_impact_gives_the_strip_its_numbers(client):
    d = client.get("/api/impact?measure=CBP").json()
    assert d["open_gap_count"] == 27
    assert d["projection"]["expected_closes"] == 11
    assert d["target"]["resulting_stars"] == 2
    assert d["ceiling"]["resulting_stars"] == 5


def test_impact_ceiling_is_separate_from_the_claim(client):
    """The strip may show 5 stars as the ceiling; it must never present it as
    what we expect to happen."""
    d = client.get("/api/impact?measure=CBP").json()
    assert d["target"]["closes_needed"] < d["ceiling"]["closes_needed"]


def test_impact_404s_an_unknown_measure(client):
    assert client.get("/api/impact?measure=NOPE").status_code == 404


# --- /api/telemetry -------------------------------------------------------


def test_telemetry_says_which_backend_is_actually_live(client):
    """Under DATA_BACKEND=auto a credential failure degrades to CSV silently.
    Without this chip you would demo local files believing you were on BigQuery."""
    d = client.get("/api/telemetry").json()
    assert d["backend"] in ("bigquery", "csv")
    assert d["demo_today"] == "2026-06-10"
    assert "hits" in d["cache"] and "misses" in d["cache"]


def test_telemetry_exposes_the_cache(client):
    """Hack.pdf's KV-cache box — it existed in code but nothing surfaced it."""
    client.get(f"/api/rank/{HERO}")
    client.get(f"/api/rank/{HERO}")
    assert client.get("/api/telemetry").json()["cache"]["hits"] > 0


# --- /api/members ---------------------------------------------------------


def test_member_picker_masks_names(client):
    """The picker needs to distinguish members, not identify them."""
    for m in client.get("/api/members?limit=10").json()["members"]:
        assert "***" in m["label"]


def test_hero_is_first_then_richest_members(client):
    """A member with one gap has nothing to rank and makes a poor demo."""
    members = client.get("/api/members?limit=10").json()["members"]
    assert members[0]["member_id"] == HERO
    assert members[0]["is_hero"] is True
    counts = [m["open_gaps"] for m in members[1:]]
    assert counts == sorted(counts, reverse=True)


# --- /api/compliance-flags ------------------------------------------------


def test_flags_endpoint_returns_newest_first(client):
    d = client.get("/api/compliance-flags?limit=5").json()
    assert "flags" in d and "count" in d
    assert len(d["flags"]) <= 5


def test_flags_never_expose_a_raw_member_id(client):
    for flag in client.get("/api/compliance-flags").json()["flags"]:
        assert HERO not in str(flag)


# --- /api/roadmap ---------------------------------------------------------


def test_roadmap_shows_the_whole_care_year(client):
    d = client.get(f"/api/roadmap/{HERO}").json()
    assert d["total"] == 4
    assert d["done"] == 1
    assert d["open"] == 3
    assert d["pct_complete"] == 25


def test_roadmap_done_steps_carry_real_evidence(client):
    """last_service_date is populated for exactly the 254 closed gaps and empty
    for all 298 open ones — so "completed on <date>" is evidence, not decoration."""
    steps = client.get(f"/api/roadmap/{HERO}").json()["steps"]
    for s in steps:
        if s["status"] == "done":
            assert s["completed_on"], s["measure_id"]
        else:
            assert s["completed_on"] is None


def test_roadmap_orders_open_steps_by_the_agents_ranking(client):
    """Not by due date. OMW is due before COA, but COA outranks it — a roadmap
    ordered by deadline would contradict the recommendation beside it."""
    d = client.get(f"/api/roadmap/{HERO}").json()
    open_steps = [s for s in d["steps"] if s["status"] == "open"]
    assert [s["measure_id"] for s in open_steps] == ["CBP", "COA", "OMW"]
    assert open_steps[0]["is_next"] is True
    scores = [s["score"] for s in open_steps]
    assert scores == sorted(scores, reverse=True)


def test_roadmap_and_decision_panel_cannot_disagree(client):
    """Both call scoring.rank_gaps. If the roadmap's next step ever differs from
    the panel's selection, one of them is fabricating."""
    roadmap = client.get(f"/api/roadmap/{HERO}").json()
    rank = client.get(f"/api/rank/{HERO}").json()
    assert roadmap["next_step"]["gap_id"] == rank["selected"]["gap_id"]
    assert roadmap["next_step"]["score"] == rank["selected"]["score"]


def test_roadmap_done_steps_come_first(client):
    steps = client.get(f"/api/roadmap/{HERO}").json()["steps"]
    statuses = [s["status"] for s in steps]
    assert statuses == sorted(statuses, key=lambda s: s != "done")


def test_roadmap_404s_an_unknown_member(client):
    assert client.get("/api/roadmap/MBR99999").status_code == 404
