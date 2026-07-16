"""Next-best-action ranking.

    score = weight x urgency x propensity

This module is pure and LLM-free on purpose. The model narrates the arithmetic;
it never performs it. That is what makes the recommendation reproducible,
unit-testable, and auditable -- a judge can recompute any number on screen.

The scoring inputs are the only care-gap-specific thing here. Swap them and the
same ranking service orders interventions for advocates or actions for providers.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

from . import config, weights

# --------------------------------------------------------------------------
# Disposition vocabulary
#
# campaign_dispositions.raw_disposition_code is deliberately messy: some rows use
# SCREAMING_SNAKE codes, others free-text CSR prose, and both encode the same
# outcomes. Normalizing it is real work, and getting it wrong silently inverts
# propensity -- "Wrong number" is not a member who declined.
# --------------------------------------------------------------------------

POSITIVE_DISPOSITIONS = frozenset(
    {
        "REPLIED_YES",
        "SCHED_CONFIRMED",
        "FORM_SUBMITTED",
        "CB_SCHED",
        "COMP_STATED",
        "MEMBER CONFIRMED APPOINTMENT SCHEDULED",
        "MEMBER REQUESTED CALLBACK",
        "MEMBER STATED ALREADY COMPLETED",
    }
)

# Contact happened, the member did not act. These count as attempts but never as
# responses. DELIVERED in particular is a carrier receipt -- the envelope arrived
# -- and reading it as engagement is how a channel nobody answers ends up looking
# like a channel that works.
NEUTRAL_DISPOSITIONS = frozenset({"DELIVERED"})

NEGATIVE_DISPOSITIONS = frozenset(
    {
        "NO_RESPONSE",
        "REFUSED",
        "OPT_OUT",
        "RETURNED_UNDELIVERABLE",
        "ABANDONED",
        "NO_ANSWER",
        "INVALID_NUM",
        "WN",
        "WRONG NUMBER",
        "DISCONNECTED",
        "PARTIAL_SUBMIT",
        "REPLIED_NO",
        "VM_LEFT",
        "NO ANSWER / VOICEMAIL",
        "LANG_BARRIER",
        "MEMBER DECLINED - NOT INTERESTED",
        "LANGUAGE BARRIER - TRANSFERRED",
    }
)

# campaign_dispositions.channel -> historical_interventions.primary_channel
CHANNEL_ALIASES = {
    "WEB FORM": "Web",
    "WEB": "Web",
    "CALL CENTER": "Call Center",
    "SMS": "SMS",
    "IVR": "IVR",
    "MAIL": "Mail",
    "PHARMACY": "Pharmacy",
    "PROVIDER": "Provider",
    "COMMUNITY": "Community",
}


def _norm(text: Any) -> str:
    """Fold prose and code variants onto one key: em/en dashes differ between
    the CSV and BigQuery encodings of the same rows."""
    return (
        str(text or "")
        .strip()
        .upper()
        .replace("—", "-")
        .replace("–", "-")
        .replace("’", "'")
    )


def normalize_channel(channel: Any) -> str:
    key = _norm(channel)
    return CHANNEL_ALIASES.get(key, str(channel or "").strip())


def classify_disposition(code: Any) -> int:
    """+1 member responded, -1 member did not, 0 contact made but no signal."""
    key = _norm(code)
    if key in POSITIVE_DISPOSITIONS:
        return 1
    if key in NEGATIVE_DISPOSITIONS:
        return -1
    return 0


def split_channels(primary_channel: Any) -> list[str]:
    """historical_interventions encodes combinations as 'IVR+Mail',
    'Provider+Web', 'Mail+Pharmacy'. Credit each leg."""
    raw = str(primary_channel or "")
    return [normalize_channel(part) for part in raw.split("+") if part.strip()]


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def urgency(days_to_close: int) -> float:
    """1 - days/horizon, clamped. A gap 40 days out scores 0.85; an overdue gap
    is maximally urgent rather than negative."""
    if days_to_close <= 0:
        return 1.0
    value = 1.0 - (days_to_close / config.URGENCY_HORIZON_DAYS)
    return max(0.05, min(1.0, round(value, 4)))


def days_to_close(due_date: Any, as_of: _dt.date | None = None) -> int | None:
    if not due_date:
        return None
    as_of = as_of or config.today()
    try:
        due = _dt.date.fromisoformat(str(due_date)[:10])
    except ValueError:
        return None
    return (due - as_of).days


def member_channel_rates(dispositions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-channel responsiveness for one member.

    Neutral and unclassified codes count toward attempts but not positives: an
    outcome like DELIVERED is evidence of contact, not of response. Response rate
    is therefore positives/attempts, not positives/(positives+negatives) -- a
    member mailed twice who never once acted scores 0.0 on mail, which is the
    truth.
    """
    tally: dict[str, dict[str, Any]] = {}
    for row in dispositions:
        channel = normalize_channel(row.get("channel"))
        if not channel:
            continue
        bucket = tally.setdefault(
            channel, {"channel": channel, "positive": 0, "negative": 0, "attempts": 0}
        )
        bucket["attempts"] += 1
        verdict = classify_disposition(row.get("raw_disposition_code"))
        if verdict > 0:
            bucket["positive"] += 1
        elif verdict < 0:
            bucket["negative"] += 1
    for bucket in tally.values():
        bucket["response_rate"] = round(bucket["positive"] / bucket["attempts"], 4)
    return tally


def measure_channel_closure(
    interventions: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Historical closure rate per (measure, channel), averaged across years."""
    acc: dict[tuple[str, str], list[float]] = {}
    costs: dict[tuple[str, str], list[float]] = {}
    for row in interventions:
        measure = row.get("measure_id")
        rate = row.get("closure_rate_pct")
        if not measure or rate is None:
            continue
        for channel in split_channels(row.get("primary_channel")):
            key = (measure, channel)
            acc.setdefault(key, []).append(float(rate) / 100.0)
            cost = row.get("cost_per_closure_usd")
            if cost is not None:
                costs.setdefault(key, []).append(float(cost))
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rates in acc.items():
        cost_list = costs.get(key, [])
        out[key] = {
            "measure_id": key[0],
            "channel": key[1],
            "closure_rate": round(sum(rates) / len(rates), 4),
            "cost_per_closure_usd": (
                round(sum(cost_list) / len(cost_list), 2) if cost_list else None
            ),
            "observations": len(rates),
        }
    return out


def propensity(
    measure_id: str,
    rates: dict[str, dict[str, Any]],
    closure: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Best achievable channel for this member on this measure.

        p(channel) = member_response_rate(channel) x measure_closure_rate(measure, channel)

    Maximizing over channels answers two questions with one formula: how likely
    this gap is to close, and which channel to use. Channels the plan has never
    run for this measure are not candidates -- we have no closure evidence for
    them.
    """
    candidates: list[dict[str, Any]] = []
    for (measure, channel), stats in closure.items():
        if measure != measure_id:
            continue
        member = rates.get(channel)
        if member and member["attempts"] > 0:
            member_rate = member["response_rate"]
            basis = (
                f"you responded {member['positive']}/{member['attempts']} "
                f"times by {channel}"
            )
            has_signal = True
        else:
            member_rate = config.NEUTRAL_CHANNEL_PRIOR
            basis = f"no {channel} history for you (assumed {member_rate:.2f})"
            has_signal = False
        value = member_rate * stats["closure_rate"]
        candidates.append(
            {
                "channel": channel,
                "propensity": round(value, 4),
                "member_response_rate": round(member_rate, 4),
                "measure_closure_rate": stats["closure_rate"],
                "cost_per_closure_usd": stats["cost_per_closure_usd"],
                "member_signal": has_signal,
                "reason": (
                    f"{basis}; {measure_id} closes "
                    f"{stats['closure_rate']:.0%} of the time via {channel}"
                ),
            }
        )

    if not candidates:
        return {
            "propensity": config.NEUTRAL_CHANNEL_PRIOR,
            "recommended_channel": None,
            "propensity_reason": (
                f"no historical intervention data for {measure_id}; "
                f"assumed {config.NEUTRAL_CHANNEL_PRIOR:.2f}"
            ),
            "channel_options": [],
        }

    candidates.sort(key=lambda c: (-c["propensity"], c["channel"]))
    best = candidates[0]
    return {
        "propensity": best["propensity"],
        "recommended_channel": best["channel"],
        "propensity_reason": best["reason"],
        "channel_options": candidates,
    }


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

MODE_FULL = "full"
MODE_MEASURE_ONLY = "measure_only"
MODE_DEGRADED = "degraded"

MODE_LABELS = {
    MODE_FULL: "weight x urgency x propensity (member response history available)",
    MODE_MEASURE_ONLY: "weight x urgency x propensity (measure-level closure rates only)",
    MODE_DEGRADED: "weight x urgency x gap age (no intervention data available)",
}

MIN_DISPOSITIONS_FOR_FULL = 2


def gap_age_factor(outreach_attempts: Any) -> float:
    """Degraded-mode stand-in for propensity.

    Without response history, repeated unanswered outreach is the only staleness
    signal available. Note this runs opposite to propensity: more failed attempts
    raises the score, because an aging gap that resists outreach needs a
    different move, not another mailer.
    """
    try:
        attempts = int(outreach_attempts or 0)
    except (TypeError, ValueError):
        attempts = 0
    return round(min(1.0, 0.3 + 0.15 * attempts), 4)


def _rejected_because(selected: dict[str, Any], other: dict[str, Any]) -> str:
    """Say the single most important reason this gap lost, in member language."""
    sel_weight = selected["components"]["weight"]
    weight = other["components"]["weight"]
    measure = other["measure_id"]
    margin = f"scored {other['score']:.2f} against {selected['score']:.2f}"

    if weight < sel_weight:
        lead = (
            f"{weights.plain_weight(measure).capitalize()} - "
            f"{weights.weight_reason(measure)}"
        )
        if weight <= 0.5:
            return f"{lead}. {margin}."
        return (
            f"{lead}, while {selected['measure_id']} is "
            f"{weights.plain_weight(selected['measure_id'])}. {margin}."
        )

    comp = other["components"]
    sel_comp = selected["components"]
    if comp.get("propensity", 0) < sel_comp.get("propensity", 0):
        return (
            f"Same weight, but we have no channel that reaches you well for it - "
            f"{comp.get('propensity_reason', 'low propensity')}. {margin}."
        )
    if comp.get("urgency", 0) < sel_comp.get("urgency", 0):
        return (
            f"Same weight, but it has {comp.get('days_to_close')} days to close "
            f"against {sel_comp.get('days_to_close')}. {margin}."
        )
    return f"Lower overall priority - {margin}."


def _decomposition_text(rows: list[dict[str, Any]], mode: str) -> str:
    """The arithmetic, on screen. A judge should be able to recompute any line."""
    third = "propensity" if mode != MODE_DEGRADED else "gap age  "
    width = max((len(r["measure_name"] or "") for r in rows), default=20)
    width = min(max(width, 20), 34)
    lines = []
    for index, row in enumerate(rows):
        comp = row["components"]
        third_value = (
            comp.get("propensity") if mode != MODE_DEGRADED else comp.get("gap_age")
        )
        name = (row["measure_name"] or row["measure_id"])[:width]
        line = (
            f"{row['measure_id']:<6} {name:<{width}} "
            f"weight {comp['weight']:.1f} x urgency {comp['urgency']:.2f} "
            f"x {third} {third_value:.2f} = {row['score']:.3f}"
        )
        if index == 0:
            line += "   <- SELECTED"
        lines.append(line)
    return "\n".join(lines)


def rank_gaps(
    member_id: str,
    open_gaps: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
    as_of: _dt.date | None = None,
) -> dict[str, Any]:
    """Rank a member's open gaps and return the full decision, including why each
    losing gap lost. Pure: every input is passed in, nothing is fetched."""
    as_of = as_of or config.today()

    if not open_gaps:
        return {
            "status": "no_open_gaps",
            "member_id": member_id,
            "as_of": as_of.isoformat(),
            "selected": None,
            "rejected": [],
            "decomposition_text": "",
            "message": "No open care gaps for this member.",
        }

    closure = measure_channel_closure(interventions)
    rates = member_channel_rates(dispositions)

    if not closure:
        mode = MODE_DEGRADED
    elif sum(b["attempts"] for b in rates.values()) >= MIN_DISPOSITIONS_FOR_FULL:
        mode = MODE_FULL
    else:
        mode = MODE_MEASURE_ONLY

    scored: list[dict[str, Any]] = []
    for gap in open_gaps:
        measure_id = gap.get("measure_id", "")
        weight = weights.weight_for(measure_id)
        dtc = days_to_close(gap.get("due_date"), as_of)
        urg = urgency(dtc) if dtc is not None else 0.5

        components: dict[str, Any] = {
            "weight": weight,
            "weight_reason": weights.weight_reason(measure_id),
            "plain_weight": weights.plain_weight(measure_id),
            "urgency": urg,
            "days_to_close": dtc,
            "due_date": gap.get("due_date"),
            "outreach_attempts": gap.get("outreach_attempts"),
        }

        if mode == MODE_DEGRADED:
            third = gap_age_factor(gap.get("outreach_attempts"))
            components["gap_age"] = third
            components["gap_age_reason"] = (
                f"{gap.get('outreach_attempts') or 0} prior outreach attempts, "
                f"still open"
            )
        else:
            prop = propensity(
                measure_id,
                rates if mode == MODE_FULL else {},
                closure,
            )
            third = prop["propensity"]
            components.update(
                {
                    "propensity": third,
                    "recommended_channel": prop["recommended_channel"],
                    "propensity_reason": prop["propensity_reason"],
                    "channel_options": prop["channel_options"],
                }
            )

        scored.append(
            {
                "gap_id": gap.get("gap_id"),
                "measure_id": measure_id,
                "measure_name": gap.get("measure_name"),
                "score": round(weight * urg * third, 4),
                "components": components,
            }
        )

    # Ties break on weight then urgency then gap_id, so the ordering is stable
    # across runs -- a demo that reorders on refresh is not auditable.
    scored.sort(
        key=lambda r: (
            -r["score"],
            -r["components"]["weight"],
            -r["components"]["urgency"],
            str(r["gap_id"]),
        )
    )

    selected = scored[0]
    rejected = []
    for other in scored[1:]:
        entry = dict(other)
        entry["rejected_because"] = _rejected_because(selected, other)
        rejected.append(entry)

    comp = selected["components"]
    reason_bits = [
        f"{comp['plain_weight']} ({comp['weight_reason']})",
        f"{comp['days_to_close']} days to close" if comp["days_to_close"] is not None else "",
        comp.get("propensity_reason") or comp.get("gap_age_reason") or "",
    ]
    selected = dict(selected)
    selected["reason"] = "; ".join(bit for bit in reason_bits if bit)

    runner_up = rejected[0]["score"] if rejected else 0.0
    return {
        "status": "ok",
        "member_id": member_id,
        "as_of": as_of.isoformat(),
        "scoring_mode": mode,
        "scoring_mode_label": MODE_LABELS[mode],
        "formula": (
            "score = weight x urgency x propensity"
            if mode != MODE_DEGRADED
            else "score = weight x urgency x gap_age"
        ),
        "selected": selected,
        "rejected": rejected,
        "margin_over_runner_up": (
            round(selected["score"] / runner_up, 2) if runner_up else None
        ),
        "considered": len(scored),
        "decomposition_text": _decomposition_text(scored, mode),
    }
