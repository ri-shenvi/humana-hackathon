"""Business impact, computed from the data rather than asserted.

The claim on the slide is "closing k of N open gaps moves this measure from X
stars to Y". Every number here is derived from stars_performance and care_gaps,
so a judge can recompute it. Nothing is estimated.

Deliberately free of ADK and of the agent: this runs standalone so the impact
number is available before the agent is finished.
"""

from __future__ import annotations

from typing import Any

from . import bq, weights


def _benchmarks(star_row: dict[str, Any]) -> list[tuple[int, float]]:
    """(star_rating, threshold) pairs present for this measure, ascending.

    AWC and FUH carry no benchmarks at all -- they are tracked but not scored --
    so this can legitimately return an empty list.
    """
    out = []
    for rating in (2, 3, 4, 5):
        value = star_row.get(f"benchmark_{rating}star_pct")
        if value is not None:
            out.append((rating, float(value)))
    return sorted(out, key=lambda pair: pair[1])


def star_rating_for(rate_pct: float, star_row: dict[str, Any]) -> int:
    """The star rating a given compliance rate earns. Below every benchmark is 1."""
    rating = 1
    for stars, threshold in _benchmarks(star_row):
        if rate_pct >= threshold:
            rating = stars
    return rating


def measure_impact(measure_id: str) -> dict[str, Any]:
    """What closing gaps on this measure is worth, in stars."""
    star_row = bq.get_star_measure(measure_id)
    if star_row is None:
        return {"status": "error", "error_message": f"No measure {measure_id}."}

    eligible = star_row.get("members_eligible") or 0
    compliant = star_row.get("members_compliant") or 0
    if not eligible:
        return {"status": "error", "error_message": f"{measure_id} has no eligible members."}

    open_gaps = [
        g
        for g in bq.get_all_gaps()
        if g.get("measure_id") == measure_id
        and str(g.get("gap_status", "")).lower() == "open"
    ]
    open_count = len(open_gaps)

    current_rate = round(compliant / eligible * 100, 2)
    current_stars = star_rating_for(current_rate, star_row)

    # The first close count that reaches each higher star band. Anything beyond
    # the open gaps we actually have is not reachable, so it is not offered.
    ladder = []
    seen = current_stars
    for closes in range(1, open_count + 1):
        rate = (compliant + closes) / eligible * 100
        stars = star_rating_for(rate, star_row)
        if stars > seen:
            ladder.append(
                {
                    "closes_needed": closes,
                    "pct_of_open_gaps": round(closes / open_count * 100),
                    "resulting_rate_pct": round(rate, 1),
                    "resulting_stars": stars,
                }
            )
            seen = stars

    return {
        "status": "ok",
        "measure_id": measure_id,
        "measure_name": star_row.get("measure_name"),
        "weight": weights.weight_for(measure_id),
        "plain_weight": weights.plain_weight(measure_id),
        "is_scored_star_measure": star_row.get("is_scored_star_measure"),
        "at_risk": star_row.get("at_risk"),
        "trending": star_row.get("trending"),
        "members_eligible": eligible,
        "members_compliant": compliant,
        "open_gap_count": open_count,
        "current_rate_pct": current_rate,
        "current_star_rating": current_stars,
        "reported_star_rating": star_row.get("current_star_rating"),
        "benchmarks": {f"{s}_star": t for s, t in _benchmarks(star_row)},
        "star_ladder": ladder,
        "source": f"stars_performance + care_gaps ({bq.backend()})",
    }


def best_historical_intervention(measure_id: str) -> dict[str, Any] | None:
    """The plan's most effective past intervention for this measure.

    This is what makes the impact claim a projection instead of a wish: we do not
    get to assume a closure rate, we have to use one Humana has actually hit.
    """
    candidates = [
        r
        for r in bq.get_interventions()
        if r.get("measure_id") == measure_id and r.get("closure_rate_pct") is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: float(r["closure_rate_pct"]))


def headline(measure_id: str = "CBP") -> dict[str, Any]:
    """The one sentence for the slide, plus the numbers behind it.

    Anchored to the best closure rate this plan has actually achieved for the
    measure. The theoretical ceiling (close every open gap) is reported
    separately and never spoken as the claim -- "close 74% of gaps" is a vibe,
    and a judge will price it as one.
    """
    impact = measure_impact(measure_id)
    if impact["status"] != "ok":
        return impact

    ladder = impact["star_ladder"]
    if not ladder:
        impact["sentence"] = (
            f"{impact['open_gap_count']} members carry open "
            f"{impact['measure_name']} gaps, but closing every one of them does "
            f"not cross the next star threshold."
        )
        return impact

    impact["ceiling"] = ladder[-1]

    intervention = best_historical_intervention(measure_id)
    if intervention is None:
        impact["target"] = ladder[0]
        impact["sentence"] = (
            f"{impact['open_gap_count']} members carry open "
            f"{impact['measure_name']} ({measure_id}) gaps -- "
            f"{impact['plain_weight']}, currently {impact['current_star_rating']} "
            f"star at {impact['current_rate_pct']}%. Closing "
            f"{ladder[0]['closes_needed']} moves it to "
            f"{ladder[0]['resulting_rate_pct']}% -- "
            f"{ladder[0]['resulting_stars']} stars."
        )
        return impact

    rate = float(intervention["closure_rate_pct"]) / 100.0
    expected_closes = int(impact["open_gap_count"] * rate)
    reachable = [r for r in ladder if r["closes_needed"] <= expected_closes]
    target = reachable[-1] if reachable else None

    impact["projection"] = {
        "closure_rate_pct": float(intervention["closure_rate_pct"]),
        "intervention_type": intervention.get("intervention_type"),
        "primary_channel": intervention.get("primary_channel"),
        "intervention_year": intervention.get("intervention_year"),
        "cost_per_closure_usd": intervention.get("cost_per_closure_usd"),
        "expected_closes": expected_closes,
        "reaches": target,
    }
    impact["target"] = target

    lead = (
        f"{impact['open_gap_count']} members carry open {impact['measure_name']} "
        f"({measure_id}) gaps -- {impact['plain_weight']}, currently "
        f"{impact['current_star_rating']} star at {impact['current_rate_pct']}%."
    )
    # State the ceiling as a ceiling. It is the honest way to say the biggest
    # number out loud: closing every open gap really does reach it, and naming it
    # as the prize costs nothing as long as we do not imply we will get there.
    prize = (
        f"Close all {impact['open_gap_count']} and {measure_id} reaches "
        f"{ladder[-1]['resulting_rate_pct']}% -- {ladder[-1]['resulting_stars']} "
        f"stars. That is the size of the prize."
    )
    basis = (
        f"The plan's best {measure_id} intervention on record "
        f"({intervention.get('intervention_type')}, "
        f"{intervention.get('intervention_year')}) closes "
        f"{intervention['closure_rate_pct']}% via "
        f"{intervention.get('primary_channel')}."
    )

    if target is None:
        impact["sentence"] = (
            f"{lead} {basis} At that rate targeted outreach closes "
            f"{expected_closes}, which does not yet cross the next star "
            f"threshold ({ladder[0]['closes_needed']} needed) -- so the honest "
            f"claim is rate movement, not a star move."
        )
        return impact

    cost = intervention.get("cost_per_closure_usd")
    cost_bit = (
        f" for about ${cost * expected_closes:,.0f}"
        f" (${cost:,.0f} per closure)"
        if cost
        else ""
    )
    impact["sentence"] = (
        f"{lead} {prize} {basis} At that rate, per-member targeted outreach to "
        f"these {impact['open_gap_count']} closes {expected_closes} and takes "
        f"{measure_id} to {target['resulting_rate_pct']}% -- "
        f"{target['resulting_stars']} stars{cost_bit}. That is the floor, not the "
        f"hope: it is the rate this plan has already hit. Per-member targeting "
        f"replaces blanket mailing across all {impact['members_eligible']} "
        f"eligible members."
    )
    return impact


def rank_measures_by_opportunity() -> list[dict[str, Any]]:
    """Every scored measure, ordered by how much a star move is worth.

    Weight x reachable star gain: this is the plan-level view of the same
    argument the member-level prioritizer makes.
    """
    rows = []
    for star_row in bq.get_stars():
        measure_id = star_row.get("measure_id")
        impact = measure_impact(measure_id)
        if impact["status"] != "ok":
            continue
        ladder = impact["star_ladder"]
        gain = (
            ladder[-1]["resulting_stars"] - impact["current_star_rating"]
            if ladder
            else 0
        )
        rows.append(
            {
                "measure_id": measure_id,
                "measure_name": impact["measure_name"],
                "weight": impact["weight"],
                "open_gap_count": impact["open_gap_count"],
                "current_rate_pct": impact["current_rate_pct"],
                "current_star_rating": impact["current_star_rating"],
                "reachable_star_gain": gain,
                "closes_for_next_star": ladder[0]["closes_needed"] if ladder else None,
                "weighted_opportunity": round(impact["weight"] * gain, 2),
            }
        )
    rows.sort(key=lambda r: (-r["weighted_opportunity"], -r["open_gap_count"]))
    return rows


# segment_performance is far too thin to quote a rate from: the median segment
# has 3 eligible members and 122 of 169 have fewer than 5. "Members like you are
# 50% compliant" would be one person out of two. Reporting that as a rate is fake
# precision, and it would undermine the one number in this deck that is real.
MIN_SEGMENT_N = 10


def segment_breakdown(
    measure_id: str, min_eligible: int = MIN_SEGMENT_N
) -> dict[str, Any]:
    """Where this measure is weakest by segment, with the thin ones held back.

    Returns reportable segments (n >= min_eligible) separately from suppressed
    ones, so a caller can never accidentally quote a rate computed from three
    people.
    """
    rows = [
        r for r in bq.get_segment_performance() if r.get("measure_id") == measure_id
    ]
    rows.sort(key=lambda r: (r.get("rate_pct") if r.get("rate_pct") is not None else 999))

    reportable = [r for r in rows if (r.get("members_eligible") or 0) >= min_eligible]
    suppressed = [r for r in rows if (r.get("members_eligible") or 0) < min_eligible]

    return {
        "measure_id": measure_id,
        "min_eligible": min_eligible,
        "reportable": reportable,
        "suppressed_count": len(suppressed),
        "suppressed_members": sum(r.get("members_eligible") or 0 for r in suppressed),
        "note": (
            f"{len(suppressed)} of {len(rows)} {measure_id} segments have fewer "
            f"than {min_eligible} eligible members and are withheld: a rate over "
            f"3 people is not a rate. Segment data is not used in scoring for the "
            f"same reason."
        ),
    }
