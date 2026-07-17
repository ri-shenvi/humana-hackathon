"""Actioner agent -- explain, find, book.

One agent, three tools: depth without three integration surfaces.

The valuable behaviours carried over from the original build and kept here:
appointments are revalidated at booking time rather than trusted from the search
results, action tools require explicit confirmation, and every action id is
derived from its inputs so a retry lands on the same record instead of creating a
second appointment.

Scheduling is simulated. Nothing is written to a real scheduling system.
"""

from __future__ import annotations

import datetime as _dt
import math
import threading
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.adk.tools.function_tool import FunctionTool

from . import bq, common, config, measures, retrieval, scoring

_ACTION_LOCK = threading.Lock()

BOOKINGS: dict[str, dict[str, Any]] = {}
CALLBACKS: dict[str, dict[str, Any]] = {}
DISPUTES: dict[str, dict[str, Any]] = {}

MAX_CHOICES = 3


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(a))


def _resolve_gap(
    gap_id: str | None, member_id: str, tool_context: ToolContext
) -> dict[str, Any] | None:
    gap_id = gap_id or tool_context.state.get("selected_gap_id")
    if not gap_id:
        return None
    gap = bq.get_gap(gap_id)
    if gap and gap.get("member_id") == member_id:
        return gap
    return None


def _claims_for_gap(member_id: str, measure_id: str) -> dict[str, Any]:
    """Claims this member filed against the CPT codes that close this measure.

    A paid claim on the measure's own CPT means the care probably happened and
    the gap is a crediting lag, not a care lag -- the stars report's own top
    recommendation is auditing exactly this. A denied claim explains why the gap
    is still open. Both change what we should say to the member, so neither
    should be invisible.
    """
    codes = set(measures.cpt_codes_for(measure_id))
    if not codes:
        return {"checked": False, "reason": f"no CPT mapping for {measure_id}"}

    rows = [
        c
        for c in bq.fetch_table("claims")
        if c.get("member_id") == member_id and c.get("cpt_code") in codes
    ]
    rows.sort(key=lambda c: str(c.get("service_date") or ""), reverse=True)

    def summarize(claim: dict[str, Any]) -> dict[str, Any]:
        return {
            "claim_id": claim.get("claim_id"),
            "service_date": claim.get("service_date"),
            "cpt_code": claim.get("cpt_code"),
            "cpt_description": claim.get("cpt_description"),
            "claim_status": claim.get("claim_status"),
            "denial_code": claim.get("denial_code") or None,
            "denial_reason": claim.get("denial_reason") or None,
            "denial_fixable": claim.get("denial_fixable"),
            "reprocessing_days_est": claim.get("reprocessing_days_est"),
            "provider_name": claim.get("provider_name"),
        }

    paid = [summarize(c) for c in rows if c.get("claim_status") == "Paid"]
    denied = [summarize(c) for c in rows if c.get("claim_status") == "Denied"]

    return {
        "checked": True,
        "cpt_codes_checked": sorted(codes),
        "claims_found": len(rows),
        "paid_claims": paid,
        "denied_claims": denied,
        "source": f"claims ({bq.backend()})",
    }


# --------------------------------------------------------------------------
# explain_gap
# --------------------------------------------------------------------------


def explain_gap(
    gap_id: str | None = None,
    member_id: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Explain what a care gap is, why it matters, and what the plan's cost rule says.

    Args:
        gap_id: The care gap identifier, for example GAP000083. Defaults to the
            gap the prioritizer selected.
        member_id: Optional member identifier. Defaults to the authenticated
            member from session state.

    Returns:
        A clear explanation, the general coverage rule for the member's
        plan, and supporting passages from real call transcripts.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        gap = _resolve_gap(gap_id, member_id, tool_context)
        if gap is None:
            return common.error_response(
                "GAP_NOT_FOUND",
                f"No open gap {gap_id} belongs to this member.",
            )
        member = bq.get_member(member_id) or {}
        rules = measures.coverage_rules_for(
            gap["measure_id"], member.get("plan_type"), bq.get_coverage_rules()
        )
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    measure_id = gap["measure_id"]
    dtc = scoring.days_to_close(gap.get("due_date"))

    # Cost rule, stated as a general rule and never as a determination. When a
    # service is not listed as covered its zeroed copay columns do not mean free,
    # so no amount is offered at all.
    cost: dict[str, Any] = {"rule_found": bool(rules)}
    if rules:
        rule = rules[0]
        cost.update(
            {
                "cpt_code": rule.get("cpt_code"),
                "cpt_description": rule.get("cpt_description"),
                "covered": rule.get("covered"),
                "prior_auth_required": rule.get("prior_auth_required"),
                "plan_type": member.get("plan_type"),
                "notes": rule.get("notes") or None,
            }
        )
        if rule.get("covered"):
            cost["copay"] = rule.get("copay")
            cost["cost_share_pct"] = rule.get("cost_share_pct")
            cost["general_rule"] = (
                f"Under a {member.get('plan_type')} plan, "
                f"{rule.get('cpt_description')} (CPT {rule.get('cpt_code')}) is "
                f"listed as covered"
                + (
                    f" with a ${rule['copay']:.0f} copay"
                    if rule.get("copay") is not None
                    else ""
                )
                + (
                    f" and {rule['cost_share_pct']:.0f}% member cost share"
                    if rule.get("cost_share_pct") is not None
                    else ""
                )
                + (
                    ". Prior authorization is required."
                    if rule.get("prior_auth_required")
                    else "."
                )
            )
        else:
            cost["copay"] = None
            cost["cost_share_pct"] = None
            cost["general_rule"] = (
                f"Under a {member.get('plan_type')} plan, "
                f"{rule.get('cpt_description')} (CPT {rule.get('cpt_code')}) is "
                f"not listed as a covered benefit. No cost-share figure applies to "
                f"an uncovered service, so no amount can be quoted here."
            )
        cost["is_determination"] = False
        cost["disclaimer"] = (
            "This is the plan's general rule, not a decision about this member's "
            "claim. Only a licensed advocate can determine coverage."
        )
    else:
        cost["general_rule"] = (
            f"This plan's rule set lists no service code for {measure_id}, so no "
            f"general cost rule can be quoted."
        )

    caveat = measures.caveat_for(measure_id)
    if caveat:
        cost["caveat"] = caveat

    passages = retrieval.search_member_language(
        f"{gap.get('measure_name')} {measures.action_for(measure_id)}", k=2
    )

    # A denied or already-paid claim on this measure's CPT usually explains why
    # the gap is still open. Telling the member to go book an appointment they
    # already had -- or that was denied for a fixable reason -- is the noise this
    # product exists to stop.
    claims = _claims_for_gap(member_id, measure_id)
    history: dict[str, Any] = {"claims_checked": claims.get("checked", False)}
    if claims.get("paid_claims"):
        newest = claims["paid_claims"][0]
        history["already_paid"] = newest
        history["note"] = (
            f"Our records show a paid claim for this service on "
            f"{newest['service_date']} ({newest['cpt_description']}). The gap may "
            f"still be open because of a crediting lag rather than missing care. "
            f"Offer to file a reconciliation request instead of booking."
        )
    elif claims.get("denied_claims"):
        newest = claims["denied_claims"][0]
        history["denied"] = newest
        history["note"] = (
            f"A claim for this service was denied on {newest['service_date']} "
            f"({newest['denial_code']}: {newest['denial_reason']})."
            + (
                " It is marked fixable, so it may only need resubmission."
                if newest.get("denial_fixable")
                else ""
            )
            + " Explain this rather than implying the member never went."
        )

    return {
        "status": "ok",
        "member_id": member_id,
        "gap_id": gap["gap_id"],
        "measure_id": measure_id,
        "measure_name": gap.get("measure_name"),
        "what_it_is": measures.action_for(measure_id),
        "due_date": gap.get("due_date"),
        "days_to_close": dtc,
        "claim_history": history,
        "why_it_matters": {
            "for_you": (
                f"This is the care step your plan has open for you, due "
                f"{gap.get('due_date')}."
            ),
            "next_step": measures.action_for(measure_id),
        },
        "cost": cost,
        "member_voices": [
            {"text": p["text"], "source": p["source"], "scenario": p["scenario"]}
            for p in passages
        ],
        "source": f"care_gaps + coverage_rules + stars_performance ({bq.backend()}) + call transcripts",
    }


# --------------------------------------------------------------------------
# find_provider
# --------------------------------------------------------------------------


def find_provider(
    gap_id: str | None = None,
    member_id: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Find the nearest in-network providers with an open appointment for this gap.

    Args:
        gap_id: The care gap identifier. Defaults to the gap the prioritizer selected.
        member_id: Optional member identifier. Defaults to the authenticated
            member from session state.

    Returns:
        Up to three ranked options with distance, next open slot, and the reasons
        each was ranked where it was.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        gap = _resolve_gap(gap_id, member_id, tool_context)
        if gap is None:
            return common.error_response(
                "GAP_NOT_FOUND", f"No open gap {gap_id} belongs to this member."
            )
        member = bq.get_member(member_id)
        if member is None:
            return common.error_response("UNKNOWN_MEMBER", f"No member {member_id}.")
        providers = {p["provider_id"]: p for p in bq.get_providers()}
        slots = bq.get_slots()
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    measure_id = gap["measure_id"]
    wanted_specialties = set(measures.specialties_for(measure_id))
    wanted_visits = set(measures.visit_types_for(measure_id))
    today = config.today().isoformat()

    excluded: dict[str, int] = {}

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    candidates: list[dict[str, Any]] = []
    for slot in slots:
        if not slot.get("available"):
            exclude("no open slot")
            continue
        if str(slot.get("slot_date")) < today:
            exclude("slot already past")
            continue
        if slot.get("network_status") != "In-Network":
            exclude("out of network")
            continue
        if wanted_specialties and slot.get("specialty") not in wanted_specialties:
            exclude("wrong specialty for this measure")
            continue
        if wanted_visits and slot.get("visit_type") not in wanted_visits:
            exclude("appointment type does not address this gap")
            continue
        provider = providers.get(slot.get("provider_id"))
        if not provider:
            exclude("unknown provider")
            continue
        if provider.get("accepting_new_patients") != "Yes":
            exclude("not accepting new patients")
            continue
        candidates.append((slot, provider))

    if not candidates:
        return {
            "status": "no_match",
            "member_id": member_id,
            "gap_id": gap["gap_id"],
            "measure_id": measure_id,
            "message": (
                f"No in-network provider currently has an open appointment that "
                f"addresses {measure_id}."
            ),
            "excluded_counts": excluded,
            "next_step": "Offer a callback from a care coordinator instead.",
        }

    # One option per provider -- the member is choosing a doctor, not a calendar
    # entry. Three slots with the same physician is one choice wearing three hats.
    member_lat, member_lon = member.get("lat"), member.get("lon")

    def slot_sort_key(pair):
        slot, _ = pair
        return (str(slot.get("slot_date")), str(slot.get("slot_time")))

    per_provider: dict[str, tuple] = {}
    for slot, provider in sorted(candidates, key=slot_sort_key):
        per_provider.setdefault(provider["provider_id"], (slot, provider))

    options = []
    for slot, provider in per_provider.values():
        distance = None
        if None not in (member_lat, member_lon, slot.get("lat"), slot.get("lon")):
            distance = round(
                haversine_miles(member_lat, member_lon, slot["lat"], slot["lon"]), 1
            )
        wait_days = (
            (_dt.date.fromisoformat(str(slot["slot_date"])) - config.today()).days
            if slot.get("slot_date")
            else None
        )
        options.append((slot, provider, distance, wait_days))

    # Scored relative to the other options rather than on an absolute scale.
    # The coordinates in this dataset are synthetic and incoherent -- the hero
    # member has a Maine address, a California zip, and Oregon coordinates -- so
    # absolute mileage means nothing and an absolute formula produces nonsense
    # (a -346 "score"). What survives the synthetic geography is the ordering:
    # "closer than the alternatives" is true even when "3 miles" is not.
    distances = [o[2] for o in options if o[2] is not None]
    waits = [o[3] for o in options if o[3] is not None]
    d_min, d_max = (min(distances), max(distances)) if distances else (0.0, 0.0)
    w_min, w_max = (min(waits), max(waits)) if waits else (0, 0)

    def relative(value, low, high):
        if value is None or high == low:
            return 0.0
        return (value - low) / (high - low)

    # Position, not mileage. "2nd closest of your 13 in-network options" is true
    # and sayable; "303 miles further than the closest" is true, useless, and
    # alarming.
    by_distance = sorted(
        (o for o in options if o[2] is not None), key=lambda o: o[2]
    )
    distance_rank = {o[1]["provider_id"]: i + 1 for i, o in enumerate(by_distance)}
    total_options = len(options)

    def ordinal(n: int) -> str:
        if 10 <= n % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"

    ranked: list[dict[str, Any]] = []
    for slot, provider, distance, wait_days in options:
        rel_distance = relative(distance, d_min, d_max)
        rel_wait = relative(wait_days, w_min, w_max)
        score = 100.0 - 45.0 * rel_distance - 45.0 * rel_wait
        if slot.get("telehealth"):
            score += 5.0

        rank = distance_rank.get(provider["provider_id"])
        reasons = [f"in-network {slot.get('specialty')}"]
        if rank == 1:
            reasons.append(f"closest of your {total_options} in-network options")
        elif rank is not None:
            reasons.append(
                f"{ordinal(rank)} closest of your {total_options} in-network options"
            )
        if wait_days is not None:
            reasons.append(
                "the soonest appointment available"
                if wait_days == w_min
                else f"available in {wait_days} days"
            )
        if slot.get("telehealth"):
            reasons.append("can be done by telehealth")
        if provider.get("hospital_affiliation"):
            reasons.append(f"affiliated with {provider['hospital_affiliation']}")

        ranked.append(
            {
                "provider_id": provider["provider_id"],
                "provider_name": provider.get("name"),
                "specialty": slot.get("specialty"),
                "network_status": slot.get("network_status"),
                "address": slot.get("address"),
                "city": slot.get("city"),
                "state": slot.get("state"),
                "zip": slot.get("zip"),
                "phone": slot.get("phone"),
                "distance_miles": distance,
                "distance_rank": rank,
                "distance_is_synthetic": True,
                "slot_id": slot.get("slot_id"),
                "slot_date": slot.get("slot_date"),
                "slot_time": slot.get("slot_time"),
                "visit_type": slot.get("visit_type"),
                "telehealth": slot.get("telehealth"),
                "days_until_appointment": wait_days,
                "score": round(min(100.0, max(0.0, score)), 1),
                "ranking_reasons": reasons,
            }
        )

    ranked.sort(
        key=lambda r: (
            -r["score"],
            str(r["slot_date"]),
            str(r["slot_time"]),
            r["provider_id"],
        )
    )
    top = ranked[:MAX_CHOICES]
    tool_context.state["last_provider_results"] = [
        {"provider_id": r["provider_id"], "slot_id": r["slot_id"]} for r in top
    ]

    return {
        "status": "ok",
        "member_id": member_id,
        "gap_id": gap["gap_id"],
        "measure_id": measure_id,
        "as_of": config.today().isoformat(),
        "recommendations": top,
        "providers_considered": len(per_provider),
        "slots_considered": len(candidates),
        "excluded_counts": excluded,
        "top_pick_reason": (
            f"{top[0]['provider_name']} ranks first: "
            f"{', '.join(top[0]['ranking_reasons'][:3])}."
        ),
        "geography_note": (
            "Coordinates in this synthetic dataset are randomly generated and do "
            "not correspond to the members' or providers' stated addresses, so "
            "absolute mileage is meaningless. Providers are therefore ranked "
            "nearest-first relative to each other, and mileage must not be quoted "
            "to the member as a real-world distance."
        ),
        "source": f"providers + appointment_slots + members ({bq.backend()})",
    }


# --------------------------------------------------------------------------
# book_or_callback
# --------------------------------------------------------------------------


def book_or_callback(
    action: str,
    gap_id: str | None = None,
    member_id: str | None = None,
    provider_id: str = "",
    slot_id: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Book a simulated appointment, or request a callback from a care coordinator.

    Requires explicit member confirmation before it runs.

    Args:
        action: Either "book" or "callback".
        gap_id: The care gap this action closes.
        member_id: Optional member identifier. Defaults to the authenticated
            member from session state.
        provider_id: Required for "book". The provider to book with.
        slot_id: Required for "book". The appointment slot to take.

    Returns:
        A confirmation receipt with a tracking id, or an error explaining why the
        action could not complete.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    action = (action or "").strip().lower()
    if action not in ("book", "callback"):
        return common.error_response(
            "INVALID_ACTION", "action must be either 'book' or 'callback'."
        )

    try:
        gap = _resolve_gap(gap_id, member_id, tool_context)
        if gap is None:
            return common.error_response(
                "GAP_NOT_FOUND", f"No open gap {gap_id} belongs to this member."
            )

        if action == "callback":
            return _request_callback(member_id, gap, tool_context)

        if not provider_id or not slot_id:
            return common.error_response(
                "MISSING_SLOT",
                "Booking needs both provider_id and slot_id. Call find_provider first.",
            )
        return _book(member_id, gap, provider_id, slot_id)
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))


def _book(
    member_id: str, gap: dict[str, Any], provider_id: str, slot_id: str
) -> dict[str, Any]:
    slot = next((s for s in bq.get_slots() if s.get("slot_id") == slot_id), None)
    provider = next(
        (p for p in bq.get_providers() if p.get("provider_id") == provider_id), None
    )
    if slot is None:
        return common.error_response("SLOT_NOT_FOUND", f"No slot {slot_id}.")
    if provider is None:
        return common.error_response("PROVIDER_NOT_FOUND", f"No provider {provider_id}.")

    # Revalidate rather than trust the search result. The search may be several
    # turns old, and a booking is the one thing here with a consequence.
    if slot.get("provider_id") != provider_id:
        return common.error_response(
            "SLOT_PROVIDER_MISMATCH",
            f"Slot {slot_id} belongs to a different provider.",
        )
    if not slot.get("available"):
        return common.error_response(
            "SLOT_UNAVAILABLE", "That appointment is no longer open."
        )
    if str(slot.get("slot_date")) < config.today().isoformat():
        return common.error_response(
            "SLOT_IN_PAST", "That appointment time has already passed."
        )
    if slot.get("network_status") != "In-Network":
        return common.error_response(
            "OUT_OF_NETWORK", "That provider is out of network for this plan."
        )
    wanted = set(measures.visit_types_for(gap["measure_id"]))
    if wanted and slot.get("visit_type") not in wanted:
        return common.error_response(
            "WRONG_VISIT_TYPE",
            f"A {slot.get('visit_type')} appointment does not close a "
            f"{gap['measure_id']} gap.",
        )

    confirmation = common.stable_id("APPT", member_id, gap["gap_id"], slot_id)
    with _ACTION_LOCK:
        existing = BOOKINGS.get(confirmation)
        if existing:
            return {**existing, "idempotent_replay": True}

        receipt = {
            "status": "ok",
            "action": "book",
            "confirmation_id": confirmation,
            "member_id": member_id,
            "gap_id": gap["gap_id"],
            "measure_id": gap["measure_id"],
            "provider_id": provider_id,
            "provider_name": provider.get("name"),
            "specialty": slot.get("specialty"),
            "address": slot.get("address"),
            "phone": slot.get("phone"),
            "appointment_date": slot.get("slot_date"),
            "appointment_time": slot.get("slot_time"),
            "visit_type": slot.get("visit_type"),
            "telehealth": slot.get("telehealth"),
            "booking_status": "booked",
            "simulated": True,
            "note": (
                "Simulated booking for the hackathon demo. No real scheduling "
                "system was contacted."
            ),
        }
        BOOKINGS[confirmation] = receipt

    common.write_audit_event(
        "appointment_booked",
        member_id,
        {
            "confirmation_id": confirmation,
            "gap_id": gap["gap_id"],
            "measure_id": gap["measure_id"],
            "provider_id": provider_id,
            "slot_id": slot_id,
            "appointment_date": slot.get("slot_date"),
        },
    )
    return receipt


def _request_callback(
    member_id: str, gap: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any]:
    channel = "Call Center"
    ranking = tool_context.state.get("last_ranking") or {}
    if ranking.get("recommended_channel"):
        channel = ranking["recommended_channel"]

    tracking = common.stable_id("CB", member_id, gap["gap_id"])
    with _ACTION_LOCK:
        existing = CALLBACKS.get(tracking)
        if existing:
            return {**existing, "idempotent_replay": True}
        receipt = {
            "status": "ok",
            "action": "callback",
            "callback_tracking_id": tracking,
            "member_id": member_id,
            "gap_id": gap["gap_id"],
            "measure_id": gap["measure_id"],
            "channel": channel,
            "callback_status": "callback_requested",
            "simulated": True,
            "note": (
                "Simulated callback request for the hackathon demo. A care "
                "coordinator would contact the member on this channel."
            ),
        }
        CALLBACKS[tracking] = receipt

    common.write_audit_event(
        "callback_requested",
        member_id,
        {
            "callback_tracking_id": tracking,
            "gap_id": gap["gap_id"],
            "measure_id": gap["measure_id"],
            "channel": channel,
        },
    )
    return receipt


# --------------------------------------------------------------------------
# submit_gap_dispute
# --------------------------------------------------------------------------


def submit_gap_dispute(
    gap_id: str | None = None,
    member_id: str | None = None,
    facility_name: str = "",
    approximate_date: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """File a reconciliation request when the member says they already had this care.

    Records the member's account for review. It never closes or alters the care
    gap: the gap stays open until the source system is reconciled, and telling
    the member otherwise would be a false assurance.

    Requires explicit member confirmation before it runs.

    Args:
        gap_id: The gap the member believes is already complete.
        member_id: Optional member identifier. Defaults to the authenticated
            member from session state.
        facility_name: Where the member says the care happened.
        approximate_date: Roughly when, in the member's own words.

    Returns:
        A tracking receipt, and an explicit statement that the gap remains open.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        gap = _resolve_gap(gap_id, member_id, tool_context)
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))
    if gap is None:
        return common.error_response(
            "GAP_NOT_FOUND", f"No open gap {gap_id} belongs to this member."
        )
    if not (facility_name or "").strip():
        return common.error_response(
            "FACILITY_REQUIRED", "Ask the member where the care was completed."
        )
    if not (approximate_date or "").strip():
        return common.error_response(
            "APPROXIMATE_DATE_REQUIRED", "Ask the member roughly when it happened."
        )

    # Look for a claim that corroborates the member before filing. If one exists,
    # the reviewer should not have to go find it, and the member deserves to hear
    # that we can see it.
    evidence = _claims_for_gap(member_id, gap["measure_id"])

    key = common.stable_id(
        "DISPUTE",
        member_id,
        gap["gap_id"],
        facility_name.casefold(),
        approximate_date.casefold(),
    )
    with _ACTION_LOCK:
        existing = DISPUTES.get(key)
        if existing:
            return {**existing, "idempotent_replay": True}
        receipt = {
            "status": "ok",
            "action": "dispute",
            "tracking_id": key,
            "member_id": member_id,
            "gap_id": gap["gap_id"],
            "measure_id": gap["measure_id"],
            "measure_name": gap.get("measure_name"),
            "facility_name": facility_name,
            "approximate_date": approximate_date,
            "review_status": "submitted_for_review",
            "underlying_gap_changed": False,
            "underlying_gap_status": gap.get("gap_status"),
            "supporting_claims": evidence,
            "simulated": True,
            "important_context": (
                "The care gap stays open while this is reviewed. Records can lag, "
                "so a missing record does not mean the care did not happen -- and "
                "this request does not by itself close the gap."
            ),
        }
        if evidence.get("paid_claims"):
            newest = evidence["paid_claims"][0]
            receipt["priority"] = "expedite"
            receipt["evidence_summary"] = (
                f"A paid claim already exists for this service "
                f"({newest['claim_id']}, {newest['cpt_description']}, "
                f"{newest['service_date']}). The care appears to have happened and "
                f"the gap looks like a crediting lag rather than a care lag."
            )
        elif evidence.get("denied_claims"):
            newest = evidence["denied_claims"][0]
            receipt["evidence_summary"] = (
                f"A claim for this service was denied ({newest['claim_id']}, "
                f"{newest['service_date']}, {newest['denial_code']}: "
                f"{newest['denial_reason']}). That is likely why the gap is still "
                f"open."
                + (
                    " The denial is marked fixable, so resubmission may close it."
                    if newest.get("denial_fixable")
                    else ""
                )
            )
        DISPUTES[key] = receipt

    common.write_audit_event(
        "gap_dispute_submitted",
        member_id,
        {
            "tracking_id": key,
            "gap_id": gap["gap_id"],
            "measure_id": gap["measure_id"],
            "underlying_gap_changed": False,
        },
    )
    return receipt


INSTRUCTION = """
You are the Actioner for CareGap Compass. Once a care gap has been selected, you
explain it, find a provider, and get it booked.

Tools:
- explain_gap: what the gap is, why it matters, and the plan's general cost rule.
- find_provider: nearest in-network providers with an appointment that actually
  addresses this gap.

You do NOT book. Booking and reconciliation requests need the member's explicit
confirmation, and the orchestrator holds those. When the member is ready to act,
hand back the exact provider_id and slot_id you recommended and let the
orchestrator take it from there. Never tell the member you have booked anything.

If the member says they already completed the care: believe them, and say that
records often lag. Ask where it happened, then roughly when -- one question at a
time -- then submit the dispute. Tell them clearly that the gap stays open until
it is reviewed. Never imply you closed it.

explain_gap returns claim_history. Read it before you tell anyone to book:
- already_paid: a claim for this exact service is already paid. Say so first.
  Do not push them to book an appointment they have already had -- offer to file
  the reconciliation instead. This is the noise this product exists to stop.
- denied: their claim was denied. Say when and why clearly, and say if
  it is fixable. Never imply they simply did not go.

How to work:
1. Use clear, direct wording at a sixth-grade reading level. Short sentences.
   Do not call attention to the reading level or label the response as simplified.
2. Offer at most three provider choices. Lead with the top pick and say why it
   is the top pick, using its ranking_reasons.
3. When you recommend a slot, state the provider_id and slot_id explicitly so
   the orchestrator can act on exactly what the member agreed to.
4. If find_provider returns no_match, say so and suggest a callback rather than
   inventing an option.

Hard rules:
- Every provider, address, time, and cost you mention must come from a tool.
  Never invent one.
- Do NOT quote distances in miles. The coordinates in this dataset are synthetic
  and do not match the stated addresses, so the mileage is not a real-world
  distance. Say "the closest of your in-network options" instead. If the member
  asks how far it is, tell them the demo data has no real geography and a
  coordinator will confirm the actual location.
- Cost: you may state the general_rule from explain_gap as general plan
  information. You may never say whether THIS member's claim will be covered, and
  you may never quote a final amount they will owe. If they push, route them to a
  licensed advocate.
- If cost.covered is false, do not present any dollar figure as what they will
  pay. An uncovered service is not a free service.
- Never diagnose, never interpret a result, never advise on medication.
- Ask one question at a time.
- Say clearly that scheduling here is simulated for the demo.
""".strip()


# Read-only tools only.
#
# The confirmation-gated actions (book_or_callback, submit_gap_dispute) are
# deliberately NOT registered here -- they live on the root agent. AgentTool runs
# this agent inside its own Runner with a throwaway InMemorySessionService and
# never yields its events to the caller, so a require_confirmation prompt raised
# in here would be emitted into a stream nobody is reading, against a session
# that is then destroyed. The client could never answer it, and AgentTool would
# return '' to the orchestrator: booking would silently no-op.
#
# So anything with a consequence is held by the orchestrator, at the same level
# as the compliance gate. This agent explains and finds; it does not act.
actioner_agent = Agent(
    name="actioner",
    model=config.MODEL,
    description=(
        "Explains a selected care gap clearly and finds the nearest "
        "in-network provider with a real open appointment. Recommends a slot for "
        "the orchestrator to book; does not act."
    ),
    instruction=INSTRUCTION,
    tools=[explain_gap, find_provider],
)
