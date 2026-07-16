"""Drive every tool directly, without the LLM.

    python -m caregap_compass.scripts.smoke_test

The model is the slow, non-deterministic part. Everything underneath it is
neither, so it can be checked in a few seconds. If this passes and the demo still
misbehaves, the problem is the prompt. If this fails, nothing above it can work.

Exit code is non-zero on the first failure, so it is usable as a pre-demo gate.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any

from .. import (
    actioner,
    agent,
    bq,
    compliance,
    config,
    feedback,
    impact,
    prioritizer,
    retrieval,
)

PASS = "  ok  "
FAIL = " FAIL "

_failures: list[str] = []


class Ctx:
    """Stands in for ADK's ToolContext: the tools only use .state."""

    def __init__(self, **state: Any) -> None:
        self.state = dict(state)


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = PASS if condition else FAIL
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        _failures.append(label)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 60 - len(title)))


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    member = config.HERO_MEMBER_ID

    section("environment")
    print(f"       backend      {bq.backend()}")
    print(f"       model        {config.MODEL}")
    print(f"       demo today   {config.today()}")
    print(f"       hero member  {member}")
    check(
        "compliance gate is enforced in code",
        agent.root_agent.before_model_callback is compliance.compliance_gate_callback,
        "before_model_callback -- a gated turn never reaches the model",
    )
    gated_tools = {
        getattr(t, "name", "")
        for t in agent.root_agent.tools
        if getattr(t, "_require_confirmation", False)
    }
    check(
        "actions are confirmation-gated on the root",
        gated_tools == {"book_or_callback", "submit_gap_dispute"},
        f"{sorted(gated_tools)} -- inside an AgentTool these could never be answered",
    )
    check(
        "demo clock is pinned to the dataset era",
        config.today().isoformat() == "2026-06-10",
        "slots run 2026-06-11..2026-07-10; the real clock would expire them all",
    )

    section("data layer")
    hero = bq.get_member(member)
    check("hero member loads", hero is not None, member)
    check("types coerced", isinstance(hero.get("age"), int), f"age={hero.get('age')!r}")
    check(
        "booleans coerced",
        isinstance(hero.get("chronic_hypertension"), bool),
        f"htn={hero.get('chronic_hypertension')!r}",
    )
    check("members table full", len(bq.list_member_ids()) == 200)
    check("care_gaps table full", len(bq.get_all_gaps()) == 552)
    gaps = bq.get_open_gaps(member)
    check("hero has 3 open gaps", len(gaps) == 3, str([g["measure_id"] for g in gaps]))
    bq.get_member(member)
    check("cache serves repeats", bq.cache_stats()["hits"] > 0, str(bq.cache_stats()))

    section("prioritizer -- the ranking layer")
    ctx = Ctx()
    ranking = prioritizer.rank_open_gaps(member, ctx)
    check("ranking returns ok", ranking.get("status") == "ok")
    check(
        "scoring mode is full",
        ranking.get("scoring_mode") == "full",
        ranking.get("scoring_mode_label", ""),
    )
    selected = ranking.get("selected") or {}
    check("CBP is selected", selected.get("measure_id") == "CBP", str(selected.get("score")))
    check(
        "margin is decisive",
        (ranking.get("margin_over_runner_up") or 0) >= 2.0,
        f"{ranking.get('margin_over_runner_up')}x over runner-up",
    )
    check("both losers are rejected", len(ranking.get("rejected", [])) == 2)
    check(
        "every rejection has a reason",
        all(r.get("rejected_because") for r in ranking.get("rejected", [])),
    )
    check("session state carries the pick", ctx.state.get("selected_measure_id") == "CBP")
    print()
    print("       " + "\n       ".join(ranking["decomposition_text"].splitlines()))
    print()
    for entry in ranking["rejected"]:
        print(f"       rejected {entry['measure_id']}: {entry['rejected_because']}")

    weight = prioritizer.get_measure_weight("CBP")
    check("CBP weighted 3x", weight.get("weight") == 3.0, weight.get("weight_reason", ""))
    check("no weight conflicts", "weight_conflict" not in weight)
    history = prioritizer.get_response_history(member, ctx)
    check(
        "response history reads channels",
        history.get("best_channel") == "Call Center",
        f"best={history.get('best_channel')} dead={history.get('dead_channels')}",
    )
    check(
        "mail is a dead channel for the hero",
        "Mail" in (history.get("dead_channels") or []),
        "mailed twice, never once acted",
    )

    section("compliance gate")
    for message in ("What should I do about my health?", "Book it for Tuesday"):
        verdict = compliance.classify_request(message)
        check(f"allowed: {message!r}", not verdict["gated"])
    for message, category in (
        ("Is my colonoscopy covered 100%?", compliance.COVERAGE_DETERMINATION),
        ("Will you pay for this?", compliance.COVERAGE_DETERMINATION),
        ("Should I stop taking my medication?", compliance.CLINICAL_ADVICE),
    ):
        verdict = compliance.classify_request(message)
        check(
            f"gated: {message!r}", verdict["gated"] and verdict["category"] == category
        )

    before = len(compliance.read_flags())
    gate_ctx = Ctx(authenticated_member_id=member, selected_measure_id="COL")
    gated = compliance.check_request("Is my colonoscopy covered 100%?", member, gate_ctx)
    check("determination refused", gated.get("allowed") is False)
    check("general rule stated", bool(gated["general_rule"].get("statement")))
    check("routed to a human", gated["route"]["route_to"] == "licensed member advocate")
    check("compliance flag written", len(compliance.read_flags()) == before + 1)
    check(
        "no cost quoted for an uncovered service",
        gated["general_rule"].get("covered") is False
        and gated["general_rule"].get("copay") is None,
        "zeroed copay columns must not be read as 'free'",
    )
    print(f"       rule: {gated['general_rule']['statement']}")
    flag = compliance.read_flags()[-1]
    check("flag masks the member", flag["entity_id"].startswith("***"), flag["entity_id"])

    authorized = compliance.check_caller_authorization("Douglas Hart", member, Ctx())
    check(
        "authorized caller allowed",
        authorized.get("authorized") is True,
        f"on file as {authorized.get('relationship')}",
    )
    stranger = compliance.check_caller_authorization("Random Stranger", member, Ctx())
    check(
        "unknown caller refused",
        stranger.get("authorized") is False and bool(stranger.get("compliance_flag_id")),
        "release of information blocked and flagged",
    )
    check(
        "refusal does not confirm the member exists",
        member not in stranger["refusal"],
    )

    section("actioner")
    gap_id = selected["gap_id"]
    explained = actioner.explain_gap(gap_id, member, ctx)
    check("explain returns ok", explained.get("status") == "ok")
    check("cost is a rule, not a decision", explained["cost"].get("is_determination") is False)
    check("grounded in transcripts", len(explained.get("member_voices", [])) > 0)
    check(
        "claims checked before telling anyone to book",
        explained.get("claim_history", {}).get("claims_checked") is True,
        "a paid claim means the care happened and the gap is a crediting lag",
    )
    print(f"       cost rule: {explained['cost']['general_rule']}")

    found = actioner.find_provider(gap_id, member, ctx)
    check("provider search returns ok", found.get("status") == "ok")
    recs = found.get("recommendations", [])
    check("at most three choices", 0 < len(recs) <= 3, f"{len(recs)} offered")
    check(
        "choices are distinct providers",
        len({r["provider_id"] for r in recs}) == len(recs),
        "three slots with one doctor is one choice, not three",
    )
    check("scores are sane", all(0 <= r["score"] <= 100 for r in recs))
    check("all in-network", all(r["network_status"] == "In-Network" for r in recs))
    check(
        "no past appointments",
        all(str(r["slot_date"]) >= config.today().isoformat() for r in recs),
    )
    for rec in recs:
        print(
            f"       {rec['provider_name'][:24]:26s} {rec['slot_date']} "
            f"{rec['slot_time']:>8}  score {rec['score']}"
        )

    top = recs[0]
    booked = actioner.book_or_callback("book", gap_id, member, top["provider_id"], top["slot_id"], ctx)
    check("booking returns a receipt", booked.get("booking_status") == "booked", booked.get("confirmation_id", ""))
    replay = actioner.book_or_callback("book", gap_id, member, top["provider_id"], top["slot_id"], ctx)
    check(
        "booking is idempotent",
        replay.get("idempotent_replay") is True
        and replay["confirmation_id"] == booked["confirmation_id"],
        "a retry must not create a second appointment",
    )

    wrong = next(
        (s for s in bq.get_slots() if s["visit_type"] == "Colonoscopy" and s["available"]),
        None,
    )
    if wrong:
        refused = actioner.book_or_callback(
            "book", gap_id, member, wrong["provider_id"], wrong["slot_id"], ctx
        )
        check(
            "revalidates at booking time",
            refused.get("error_code") == "WRONG_VISIT_TYPE",
            "a colonoscopy slot cannot close a blood-pressure gap",
        )

    called_back = actioner.book_or_callback("callback", gap_id, member, "", "", ctx)
    check("callback returns a receipt", called_back.get("callback_status") == "callback_requested")

    dispute = actioner.submit_gap_dispute(gap_id, member, "Peterland Clinic", "last March", ctx)
    check("dispute accepted", dispute.get("review_status") == "submitted_for_review")
    check(
        "dispute never closes the gap",
        dispute.get("underlying_gap_changed") is False,
        "the gap stays open pending reconciliation",
    )

    section("authorization")
    check(
        "cross-member access refused",
        prioritizer.get_open_gaps("MBR00001", ctx).get("error_code") == "MEMBER_SESSION_MISMATCH",
    )
    check(
        "unknown member refused",
        prioritizer.get_open_gaps("MBR99999", Ctx()).get("error_code") == "UNKNOWN_MEMBER",
    )

    section("retrieval + feedback")
    stats = retrieval.stats()
    check("corpus indexed", stats["passages"] > 100, str(stats["by_type"]))
    hits = retrieval.search("blood pressure care gap", 2)
    check("search returns sourced passages", all(h.get("source") for h in hits))
    import re

    check(
        "no raw member ids in the index",
        not any(re.search(r"MBR\d{5}", h["text"]) for h in retrieval.search("member id", 5)),
    )

    recorded = feedback.record_feedback(True, member, "This was clear", ctx)
    check("feedback stored", recorded.get("status") == "ok", f"id={recorded.get('feedback_id')}")
    check("feedback summary reads", feedback.summary()["total"] > 0, str(feedback.summary()))

    section("impact")
    head = impact.headline("CBP")
    check("impact computes", head.get("status") == "ok")
    check("27 open CBP gaps", head["open_gap_count"] == 27)
    check(
        "projection anchored to a real intervention",
        head["projection"]["closure_rate_pct"] == 41.7,
        head["projection"]["intervention_type"],
    )
    check(
        "claim never outruns history",
        head["projection"]["expected_closes"] / head["open_gap_count"] * 100
        <= head["projection"]["closure_rate_pct"],
    )
    print()
    print(f"       {head['sentence']}")

    print()
    print("=" * 68)
    if _failures:
        print(f"  {len(_failures)} FAILED:")
        for name in _failures:
            print(f"    - {name}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - surface the traceback, fail loudly
        traceback.print_exc()
        raise SystemExit(2)
