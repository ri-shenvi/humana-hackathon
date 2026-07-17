"""Prioritizer agent -- the ranking layer.

The obvious build for this problem is a chatbot that lists your open care gaps.
That is the thing that already doesn't work: a mailer is a list. This agent
decides which single gap matters most right now and explains the next step
without exposing the scoring machinery to the member.

It is not care-gap-specific machinery. Swap the scoring inputs and the same
service ranks interventions for advocates or actions for providers -- a
next-best-action ranking service that happens to be pointed at care gaps.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent
from google.adk.tools import ToolContext

from . import bq, common, config, scoring, weights


def get_open_gaps(member_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Return the member's open care gaps with days-to-close for each.

    Args:
        member_id: The member identifier, for example MBR00030.

    Returns:
        Open gaps with measure, due date, days to close, and prior outreach count.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        gaps = bq.get_open_gaps(member_id)
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    as_of = config.today()
    rows = []
    for gap in gaps:
        dtc = scoring.days_to_close(gap.get("due_date"), as_of)
        rows.append(
            {
                "gap_id": gap.get("gap_id"),
                "measure_id": gap.get("measure_id"),
                "measure_name": gap.get("measure_name"),
                "due_date": gap.get("due_date"),
                "days_to_close": dtc,
                "overdue": dtc is not None and dtc < 0,
                "outreach_attempts": gap.get("outreach_attempts"),
            }
        )
    rows.sort(key=lambda r: (r["days_to_close"] is None, r["days_to_close"]))

    return {
        "status": "ok",
        "member_id": member_id,
        "as_of": as_of.isoformat(),
        "open_gap_count": len(rows),
        "open_gaps": rows,
        "source": f"care_gaps ({bq.backend()})",
    }


def get_measure_weight(measure_id: str) -> dict[str, Any]:
    """Return the CMS Star weight for a measure and the plan's current standing.

    Args:
        measure_id: Measure identifier, for example CBP, CDC-H, COL, OMW, COA.

    Returns:
        Weight, the measure class it derives from, and live plan rate/star/trend.
    """
    try:
        star_row = bq.get_star_measure(measure_id)
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    if star_row is None and measure_id not in weights.MEASURE_WEIGHTS:
        return common.error_response(
            "UNKNOWN_MEASURE", f"No measure {measure_id} in stars_performance."
        )

    info = weights.describe(measure_id, star_row)
    info["status"] = "ok"
    info["source"] = f"stars_performance ({bq.backend()}) + CMS measure class"
    return info


def get_response_history(member_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Return which outreach channels this member actually responds to, and how
    often each measure closes on each channel.

    Args:
        member_id: The member identifier, for example MBR00030.

    Returns:
        Per-channel response rates for the member plus plan-wide closure rates.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        dispositions = bq.get_dispositions_for_member(member_id)
        interventions = bq.get_interventions()
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    rates = scoring.member_channel_rates(dispositions)
    closure = scoring.measure_channel_closure(interventions)

    channels = sorted(
        rates.values(), key=lambda r: (-r["response_rate"], -r["attempts"])
    )
    for channel in channels:
        channel["verdict"] = (
            "responds here"
            if channel["response_rate"] >= 0.5
            else "rarely responds here"
            if channel["response_rate"] > 0
            else "never responded here"
        )

    by_measure: dict[str, list[dict[str, Any]]] = {}
    for (measure, channel), stats in closure.items():
        by_measure.setdefault(measure, []).append(
            {
                "channel": channel,
                "closure_rate": stats["closure_rate"],
                "cost_per_closure_usd": stats["cost_per_closure_usd"],
            }
        )
    for options in by_measure.values():
        options.sort(key=lambda o: -o["closure_rate"])

    return {
        "status": "ok",
        "member_id": member_id,
        "attempts_total": sum(c["attempts"] for c in channels),
        "member_channels": channels,
        "best_channel": channels[0]["channel"] if channels else None,
        "dead_channels": [c["channel"] for c in channels if c["response_rate"] == 0],
        "measure_closure_rates": by_measure,
        "source": (
            f"campaign_dispositions + historical_interventions ({bq.backend()})"
        ),
    }


def rank_open_gaps(member_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Rank every open gap and select the single next best action.

    Scores each open gap internally, selects the highest, and returns the
    details needed by the presenter-only Insights panel.

    Args:
        member_id: The member identifier, for example MBR00030.

    Returns:
        The selected gap, rejected gaps, and internal audit fields. Do not show
        scoring details to the member unless they explicitly ask for ranking
        details.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        result = scoring.rank_gaps(
            member_id,
            bq.get_open_gaps(member_id),
            bq.get_dispositions_for_member(member_id),
            bq.get_interventions(),
        )
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    if result.get("selected"):
        selected = result["selected"]
        tool_context.state["selected_gap_id"] = selected["gap_id"]
        tool_context.state["selected_measure_id"] = selected["measure_id"]
        tool_context.state["last_ranking"] = {
            "measure_id": selected["measure_id"],
            "score": selected["score"],
            "scoring_mode": result["scoring_mode"],
        }
        common.write_audit_event(
            "gap_ranked",
            member_id,
            {
                "selected_measure_id": selected["measure_id"],
                "selected_gap_id": selected["gap_id"],
                "score": selected["score"],
                "scoring_mode": result["scoring_mode"],
                "rejected_measure_ids": [r["measure_id"] for r in result["rejected"]],
            },
        )

    result["source"] = (
        f"care_gaps + stars_performance + campaign_dispositions + "
        f"historical_interventions ({bq.backend()})"
    )
    return result


INSTRUCTION = """
You are the Prioritizer for CareGap Compass. You decide which single open care
gap matters most for this member right now.

Your job is ranking, not listing. Never present the member's gaps as a menu.

How to answer "what should I do about my health?":
1. Call rank_open_gaps. Never assert a pick without calling it first.
2. Keep the answer member-facing. Do not display formulas, weights, numeric
   rankings, internal scoring fields, or comparisons against rejected gaps.
3. State the selected gap in everyday terms. Use the measure name and the
   selected.components fields only to shape the explanation, not to expose the
   internal math.
4. Explain why this care step is a good next step in one or two short sentences:
   what it checks, why it matters, and whether it is coming up soon.
5. End with a next step: offer to explain the care step or help schedule an
   appointment.

Supporting tools:
- get_open_gaps: the raw open gaps with days-to-close.
- get_measure_weight: technical ranking context. Use only when the member asks
  for ranking details.
- get_response_history: which channels this member answers, and how often each
  measure closes on each channel.

Use them when the member asks a follow-up like "why is that one worth more?" or
"why not the other one?" -- answer from tool output, never from memory, and keep
technical ranking details out of the response unless the member explicitly asks
for them.

Rules:
- Every number you say must come from a tool. Never compute a score yourself.
- Never invent a gap, a measure, a weight, or a channel.
- If scoring_mode is not "full", say plainly which inputs were thin -- for
  example "I don't have your response history, so I ranked on weight and
  deadline alone."
- Do not give clinical advice, diagnose, or interpret results. Explain what the
  measure is and why it is scheduled.
- Do not promise coverage or cost. That is not your decision to make.
- If the member asks for technical details, tell them those are available in
  Insights rather than putting the evidence into the chat.
""".strip()


prioritizer_agent = Agent(
    name="prioritizer",
    model=config.MODEL,
    description=(
        "Ranks a member's open care gaps and selects the single next best action "
        "with member-facing wording."
    ),
    instruction=INSTRUCTION,
    tools=[rank_open_gaps, get_open_gaps, get_measure_weight, get_response_history],
)
