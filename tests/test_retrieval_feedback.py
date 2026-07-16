"""Retrieval, feedback, and the PII boundary.

The load-bearing claim across all three: nothing identifying leaves the system.
Not into the search index, not into the feedback store, not into a log line.
"""

from __future__ import annotations

import re

import pytest

from caregap_compass import config, feedback, privacy, retrieval

HERO = config.HERO_MEMBER_ID
MEMBER_ID_PATTERN = re.compile(r"\bMBR\d{5}\b")
CLAIM_ID_PATTERN = re.compile(r"\b(CLM|GAP|DISP|AUTH)\d{6}\b")


class Ctx:
    def __init__(self, **state):
        self.state = dict(state)


# --- retrieval -------------------------------------------------------------


def test_corpus_is_indexed():
    stats = retrieval.stats()
    assert stats["passages"] > 100
    assert stats["by_type"]["call_transcript"] > 0
    assert stats["by_type"]["stars_report"] > 0


def test_search_returns_sourced_passages():
    """A claim the agent cannot attribute is a claim it should not make."""
    hits = retrieval.search("blood pressure care gap outreach", 3)
    assert hits
    for hit in hits:
        assert hit["source"]
        assert hit["score"] > 0


def test_results_are_ranked():
    hits = retrieval.search("prior authorization denied", 3)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_no_member_or_claim_ids_survive_indexing():
    """The transcripts are full of real-shaped identifiers. What is never indexed
    cannot be echoed back into an answer."""
    for passage in retrieval.index().passages:
        assert not MEMBER_ID_PATTERN.search(passage["text"]), passage["source"]
        assert not CLAIM_ID_PATTERN.search(passage["text"]), passage["source"]


def test_scrubbing_preserves_the_masked_form():
    hits = retrieval.search("denied claim member", 5)
    assert any("***" in h["text"] for h in hits)


def test_member_language_excludes_the_executive_report():
    """The stars report is written for executives; lifting its phrasing into a
    member conversation is the wrong register."""
    for hit in retrieval.search_member_language("Controlling Blood Pressure", 2):
        assert hit["source_type"] == "call_transcript"


def test_empty_query_returns_nothing_rather_than_everything():
    assert retrieval.search("", 3) == []
    assert retrieval.search("the and of", 3) == []  # stopwords only


# --- privacy ---------------------------------------------------------------


def test_member_id_masking():
    assert privacy.mask_member_id("MBR00030") == "***030"
    assert privacy.mask_member_id(None) == "***"


def test_pii_fields_are_masked():
    from caregap_compass import bq

    masked = privacy.mask_pii(bq.get_member(HERO))
    assert masked["member_id"] == "***030"
    assert masked["first_name"].endswith("***")
    assert masked["email"].startswith("***@")
    assert masked["phone"].startswith("***-")
    assert masked["dob"].endswith("-**-**")
    # Quasi-identifiers stay: provider matching needs them and they do not name
    # anyone on their own.
    assert masked["age"] == 79
    assert masked["plan_type"] == "DSNP"


def test_anonymize_is_recursive():
    payload = {"member_id": "MBR00030", "nested": [{"email": "a@b.com"}]}
    clean = privacy.anonymize_for_log(payload)
    assert clean["member_id"] == "***030"
    assert clean["nested"][0]["email"] == "***@b.com"


def test_display_name_is_the_only_pii_the_agent_speaks():
    assert privacy.member_display_name({"first_name": "Donna"}) == "Donna"
    assert privacy.member_display_name({}) == "there"


# --- feedback --------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FEEDBACK_DB", tmp_path / "feedback.db")
    monkeypatch.setattr(config, "AUDIT_FILE", tmp_path / "audit.jsonl")


def test_feedback_records_and_summarizes():
    ctx = Ctx(
        authenticated_member_id=HERO,
        selected_gap_id="GAP000083",
        last_ranking={"measure_id": "CBP", "score": 0.6555, "scoring_mode": "full"},
    )
    result = feedback.record_feedback(True, HERO, "This was clear", ctx)
    assert result["status"] == "ok"

    summary = feedback.summary()
    assert summary["total"] == 1
    assert summary["helpful"] == 1
    assert summary["helpful_pct"] == 100.0


def test_feedback_never_stores_a_raw_member_id():
    ctx = Ctx(authenticated_member_id=HERO, last_ranking={"measure_id": "CBP"})
    feedback.record_feedback(False, HERO, "not useful", ctx)

    import sqlite3

    conn = sqlite3.connect(config.FEEDBACK_DB)
    row = conn.execute("SELECT * FROM feedback").fetchone()
    conn.close()
    blob = " ".join(str(v) for v in row)
    assert HERO not in blob
    assert "***030" in blob


def test_feedback_captures_the_ranking_that_produced_it():
    """'The member rejected this' is only useful if you know what was
    recommended and what it scored."""
    ctx = Ctx(
        authenticated_member_id=HERO,
        last_ranking={"measure_id": "CBP", "score": 0.6555, "scoring_mode": "full"},
    )
    feedback.record_feedback(False, HERO, "already did it", ctx)
    rows = feedback.acceptance_by_measure()
    assert rows[0]["measure_id"] == "CBP"
    assert rows[0]["avg_score"] == pytest.approx(0.6555, abs=0.001)
    assert rows[0]["helpful_pct"] == 0.0


def test_acceptance_by_measure_surfaces_the_worst_first():
    """This is the loop: a measure the ranking keeps selecting and members keep
    declining is a propensity signal the scoring does not have yet."""
    ctx_bad = Ctx(authenticated_member_id=HERO, last_ranking={"measure_id": "OMW"})
    ctx_good = Ctx(authenticated_member_id=HERO, last_ranking={"measure_id": "CBP"})
    feedback.record_feedback(False, HERO, "", ctx_bad)
    feedback.record_feedback(True, HERO, "", ctx_good)
    assert feedback.acceptance_by_measure()[0]["measure_id"] == "OMW"


def test_feedback_refuses_an_unauthenticated_member():
    ctx = Ctx(authenticated_member_id=HERO)
    assert (
        feedback.record_feedback(True, "MBR00001", "", ctx)["error_code"]
        == "MEMBER_SESSION_MISMATCH"
    )
