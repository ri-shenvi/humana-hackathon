"""The compliance gate.

The agent knows which questions it is not allowed to answer. It explains and
routes; it never decides. Coverage determinations stay with a licensed human.

Three things happen on a gated request, in this order and without exception:
state the general rule from coverage_rules, refuse the determination, route to a
human advocate. The refusal is then written to a compliance flag, because a
control that leaves no evidence is not a control.

The classifier fails closed: an ambiguous request escalates. A false escalation
costs a member thirty seconds; a false clearance is an unlicensed entity making a
coverage determination.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

from google.adk.models import LlmResponse
from google.adk.tools import ToolContext
from google.genai import types

from . import bq, common, config, measures, privacy

logger = logging.getLogger(__name__)

_FLAG_LOCK = threading.Lock()

COVERAGE_DETERMINATION = "coverage_determination"
CLINICAL_ADVICE = "clinical_advice"
IN_SCOPE = "in_scope"

# Phrases that ask us to decide, rather than to explain. Matched on a normalized
# lowercase string.
COVERAGE_SIGNALS = (
    "is this covered",
    "is it covered",
    "am i covered",
    "will you cover",
    "will you pay",
    "do you pay",
    "will insurance cover",
    "covered 100",
    "covered at 100",
    "fully covered",
    "cover the whole",
    "out of pocket",
    "will i owe",
    "do i owe",
    "how much will i pay",
    "what will it cost me",
    "approve",
    "approved",
    "authorize",
    "authorization",
    "prior auth",
    "preauth",
    "pre-auth",
    "deny",
    "denied",
    "denial",
    "appeal",
    "reimburse",
    "claim will",
    "guarantee",
)

CLINICAL_SIGNALS = (
    "should i take",
    "should i stop",
    "do i have",
    "diagnose",
    "diagnosis",
    "what's wrong with me",
    "whats wrong with me",
    "prescribe",
    "prescription for",
    "what dose",
    "dosage",
    "how much medication",
    "is it serious",
    "am i dying",
    "should i go to the er",
    "instead of my medication",
    "stop taking",
)

# Explaining a rule is in scope; asking us to apply it is not. These phrasings
# read as coverage words but are requests for education.
EDUCATIONAL_SIGNALS = (
    "how does coverage work",
    "what does coverage mean",
    "in general",
    "generally speaking",
    "what is prior auth",
    "what does prior auth mean",
    "why do you need authorization",
    "what is a copay",
    "what does copay mean",
)


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().replace("’", "'").split())


def classify_request(text: str) -> dict[str, Any]:
    """Deterministic prefilter over the member's message.

    This runs before any routing decision. It is intentionally keyword-based and
    intentionally blunt: the orchestrator's own judgement is the second layer,
    and two crude layers that fail closed beat one clever layer that fails open.
    """
    normalized = _normalize(text)
    if not normalized:
        return {
            "category": IN_SCOPE,
            "confidence": 0.0,
            "signals": [],
            "gated": False,
        }

    educational = [s for s in EDUCATIONAL_SIGNALS if s in normalized]
    coverage = [s for s in COVERAGE_SIGNALS if s in normalized]
    clinical = [s for s in CLINICAL_SIGNALS if s in normalized]

    if clinical:
        return {
            "category": CLINICAL_ADVICE,
            "confidence": round(min(0.6 + 0.2 * len(clinical), 0.99), 2),
            "signals": clinical,
            "gated": True,
        }

    if coverage:
        # An educational framing lowers confidence but never clears the gate on
        # its own: "in general, will you pay for this?" is still a determination
        # request wearing a hedge.
        confidence = min(0.6 + 0.2 * len(coverage), 0.99)
        if educational:
            confidence = max(confidence - 0.25, 0.5)
        return {
            "category": COVERAGE_DETERMINATION,
            "confidence": round(confidence, 2),
            "signals": coverage,
            "educational_framing": bool(educational),
            "gated": True,
        }

    return {"category": IN_SCOPE, "confidence": 0.0, "signals": [], "gated": False}


def log_compliance_flag(
    flag_type: str,
    severity: str,
    entity_type: str,
    entity_id: str,
    description: str,
    recommended_action: str,
    metric_label: str | None = None,
    metric_value: float | None = None,
) -> dict[str, Any]:
    """Append one flag, shaped exactly like the compliance_flags table.

    Written locally rather than to BigQuery: the hackathon dataset may be
    read-only, and a control that breaks the demo when it fires is worse than no
    control. The schema matches, so `bq load` lifts this file straight into the
    real table wherever writes are permitted.
    """
    flag = {
        "flag_id": f"FLAG-{uuid.uuid4().hex[:10].upper()}",
        "flag_type": flag_type,
        "severity": severity,
        "entity_type": entity_type,
        "entity_id": (
            privacy.mask_member_id(entity_id) if entity_type == "member" else entity_id
        ),
        "flag_date": config.today().isoformat(),
        "metric_value": metric_value,
        "metric_label": metric_label,
        "description": description,
        "recommended_action": recommended_action,
        "resolved": False,
        "source": "caregap_compass_agent",
        "synthetic": True,
    }
    config.COMPLIANCE_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _FLAG_LOCK:
        with config.COMPLIANCE_FLAG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(flag) + "\n")
    return flag


def _general_rule(member: dict[str, Any], measure_id: str | None) -> dict[str, Any]:
    """The general rule for this measure under this member's plan.

    Deliberately returns the rule, not a verdict. "Diagnostic colonoscopy is not
    listed as covered for DSNP" is a rule. "Your colonoscopy isn't covered" is a
    determination, and that is not ours to state.
    """
    plan_type = member.get("plan_type")
    if not measure_id:
        return {
            "rule_found": False,
            "statement": (
                f"Coverage under a {plan_type} plan depends on the specific "
                f"service code, the provider's network status, and whether prior "
                f"authorization applies."
            ),
        }

    rules = measures.coverage_rules_for(measure_id, plan_type, bq.get_coverage_rules())
    caveat = measures.caveat_for(measure_id)

    if not rules:
        return {
            "rule_found": False,
            "measure_id": measure_id,
            "plan_type": plan_type,
            "statement": (
                f"This plan's rule set does not list a service code for "
                f"{measure_id}, so no general cost rule can be quoted for it."
            ),
            "caveat": caveat,
        }

    rule = rules[0]
    covered = rule.get("covered")
    lead = (
        f"For a {plan_type} plan, the listed rule for {rule.get('cpt_description')} "
        f"(CPT {rule.get('cpt_code')}) is:"
    )

    if covered:
        parts = ["listed as covered"]
        if rule.get("prior_auth_required"):
            parts.append("prior authorization is required")
        if rule.get("copay") is not None:
            parts.append(f"copay ${rule['copay']:.0f}")
        if rule.get("cost_share_pct") is not None:
            parts.append(f"member cost share {rule['cost_share_pct']:.0f}%")
    else:
        # When a service is not listed as covered, its copay and cost-share
        # columns are zero because the rule does not apply -- not because the
        # service is free. Quoting them here would state the exact opposite of
        # the truth, so they are withheld and the advocate resolves the amount.
        parts = ["not listed as a covered benefit for this plan"]
        if rule.get("prior_auth_required"):
            parts.append("prior authorization is required")
        parts.append(
            "no cost-share figures apply to an uncovered service, so I can't "
            "quote you an amount"
        )

    return {
        "rule_found": True,
        "measure_id": measure_id,
        "plan_type": plan_type,
        "cpt_code": rule.get("cpt_code"),
        "cpt_description": rule.get("cpt_description"),
        "covered": covered,
        "prior_auth_required": rule.get("prior_auth_required"),
        # Withheld when not covered: see the comment above.
        "copay": rule.get("copay") if covered else None,
        "cost_share_pct": rule.get("cost_share_pct") if covered else None,
        "notes": rule.get("notes") or None,
        "statement": lead + " " + "; ".join(parts) + ".",
        "caveat": caveat,
        "source": f"coverage_rules ({bq.backend()})",
    }


ADVOCATE_ROUTE = {
    "route_to": "licensed member advocate",
    "channel": "Member Services",
    "why": (
        "A coverage determination for your specific claim can only be made by a "
        "licensed human advocate. I can explain the general rule; I cannot decide "
        "your case."
    ),
}

CLINICIAN_ROUTE = {
    "route_to": "licensed clinician",
    "channel": "your care team or nurse line",
    "why": (
        "Anything about your diagnosis, your medication, or whether a symptom is "
        "serious has to come from a licensed clinician who knows your history."
    ),
}


def check_request(
    member_message: str, member_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Screen a member message before acting on it. Call this first, every turn.

    Args:
        member_message: The member's message, verbatim.
        member_id: The member identifier, for example MBR00030.

    Returns:
        allowed=True to proceed, or allowed=False with the general rule to state,
        the refusal to make, and where to route. When gated, a compliance flag is
        already written.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    verdict = classify_request(member_message)

    if not verdict["gated"]:
        return {
            "status": "ok",
            "allowed": True,
            "category": IN_SCOPE,
            "confidence": verdict["confidence"],
        }

    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    measure_id = measures.detect_measure(member_message) or tool_context.state.get(
        "selected_measure_id"
    )
    return _build_gate_response(verdict, member_id, measure_id)


def _build_gate_response(
    verdict: dict[str, Any], member_id: str, measure_id: str | None
) -> dict[str, Any]:
    """Assemble the refusal: state the rule, refuse, route, log.

    Shared by the tool and the before-model callback so there is exactly one
    definition of what a refusal is. Two implementations would eventually
    disagree, and the one that drifts is the one that leaks.
    """
    member = bq.get_member(member_id) or {}

    if verdict["category"] == CLINICAL_ADVICE:
        flag = log_compliance_flag(
            flag_type="clinical_advice_request_refused",
            severity="medium",
            entity_type="member",
            entity_id=member_id,
            description=(
                "Member asked the agent for clinical advice. Agent refused and "
                f"routed to a clinician. Signals: {', '.join(verdict['signals'])}."
            ),
            recommended_action="Clinician follow-up with the member.",
            metric_label="classifier_confidence",
            metric_value=verdict["confidence"],
        )
        return {
            "status": "ok",
            "allowed": False,
            "category": CLINICAL_ADVICE,
            "confidence": verdict["confidence"],
            "signals": verdict["signals"],
            "refusal": (
                "I can't give medical advice or tell you what a result means -- "
                "I'm not a clinician and I don't know your history."
            ),
            "route": CLINICIAN_ROUTE,
            "compliance_flag_id": flag["flag_id"],
            "instructions_to_agent": (
                "Refuse the clinical question plainly, route to a clinician, then "
                "offer to help with what you can do: explaining the care gap and "
                "getting the appointment booked."
            ),
        }

    rule = _general_rule(member, measure_id)
    flag = log_compliance_flag(
        flag_type="coverage_determination_request_refused",
        severity="high",
        entity_type="member",
        entity_id=member_id,
        description=(
            "Member requested a coverage determination. Agent stated the general "
            "rule, refused the determination, and routed to a licensed advocate. "
            f"Signals: {', '.join(verdict['signals'])}."
            + (f" Measure in context: {measure_id}." if measure_id else "")
        ),
        recommended_action="Licensed advocate to contact the member and adjudicate.",
        metric_label="classifier_confidence",
        metric_value=verdict["confidence"],
    )

    common.write_audit_event(
        "coverage_determination_refused",
        member_id,
        {
            "measure_id": measure_id,
            "confidence": verdict["confidence"],
            "flag_id": flag["flag_id"],
        },
    )

    return {
        "status": "ok",
        "allowed": False,
        "category": COVERAGE_DETERMINATION,
        "confidence": verdict["confidence"],
        "signals": verdict["signals"],
        "general_rule": rule,
        "refusal": (
            "I can't tell you whether your specific claim will be covered or what "
            "you'll owe. That decision belongs to a licensed advocate, not to me."
        ),
        "route": ADVOCATE_ROUTE,
        "compliance_flag_id": flag["flag_id"],
        "instructions_to_agent": (
            "Do all three, in this order, and do not skip any: (1) state the "
            "general_rule.statement as general plan information, including its "
            "caveat if present; (2) refuse the determination for this member's "
            "specific case using the refusal text; (3) route to the advocate in "
            "route. Never say the words 'covered' or 'not covered' about THIS "
            "member's claim. Never quote a final amount they will owe. Then offer "
            "to continue with what you can do."
        ),
    }


def read_flags() -> list[dict[str, Any]]:
    """Every flag this agent has written. Used by the smoke test and the demo."""
    if not config.COMPLIANCE_FLAG_FILE.exists():
        return []
    with config.COMPLIANCE_FLAG_FILE.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --------------------------------------------------------------------------
# The gate as a framework callback
# --------------------------------------------------------------------------


def _last_user_text(llm_request: Any) -> str:
    """The most recent thing the member actually typed.

    Read backwards over the request contents for the last user turn carrying
    real text. Function responses also arrive with role='user' (that is how ADK
    returns tool results and confirmations), and those are not member speech --
    gating on them would fire the classifier against our own tool output.
    """
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None) or []
        if any(getattr(p, "function_response", None) for p in parts):
            continue
        text = " ".join(p.text for p in parts if getattr(p, "text", None))
        if text.strip():
            return text
    return ""


def _refusal_text(gate: dict[str, Any]) -> str:
    """The exact words spoken on a gated turn.

    Templated on purpose. The model is never called here, so this text cannot be
    talked out of, drifted away from, or prompt-injected around -- the refusal is
    identical every time and is therefore auditable.
    """
    lines: list[str] = []

    if gate["category"] == COVERAGE_DETERMINATION:
        rule = gate.get("general_rule") or {}
        if rule.get("statement"):
            lines.append(rule["statement"])
            if rule.get("caveat"):
                lines.append(rule["caveat"])
            lines.append("")
        lines.append(gate["refusal"])
        route = gate["route"]
        lines.append(
            f"I'm handing this to a {route['route_to']} through {route['channel']}. "
            f"{route['why']}"
        )
    else:
        lines.append(gate["refusal"])
        route = gate["route"]
        lines.append(f"Please speak with {route['channel']}. {route['why']}")

    lines.append("")
    lines.append(
        "I can still help you with what your plan has open for you, and I can "
        "get it booked. Would you like me to?"
    )
    return "\n".join(lines)


def compliance_gate_callback(callback_context: Any, llm_request: Any) -> Any:
    """Enforce the compliance gate before the model is ever called.

    Registered as `before_model_callback` on the root agent. Returning an
    LlmResponse here skips the model call entirely and returns this content to
    the member.

    Why a callback and not a tool: a tool only runs if the model chooses to call
    it. That makes the control advisory -- one distracted turn, one clever
    reframing, and the gate silently does not exist. Here the classifier runs on
    every turn as code, before the model sees the message, and a gated request
    never reaches the model at all. The agent cannot be talked into a coverage
    determination it was never asked to make.

    The instruction block remains as a second layer for phrasings the keywords
    miss. Two crude layers that fail closed beat one clever layer that fails open.
    """
    message = _last_user_text(llm_request)
    verdict = classify_request(message)
    if not verdict["gated"]:
        callback_context.state["last_gate"] = {"gated": False}
        return None

    # This callback runs before every model call, and one user turn can trigger
    # several (model -> tool -> model). Without this guard the same message would
    # be re-classified and re-flagged on each pass, inflating the flag log with
    # duplicates of a single refusal.
    invocation_id = getattr(callback_context, "invocation_id", None)
    already = callback_context.state.get("_gated_invocation")
    if invocation_id and already == invocation_id:
        return None

    member_id = (
        callback_context.state.get("authenticated_member_id") or config.HERO_MEMBER_ID
    )
    # What they asked about beats what we happened to select. A member whose
    # selected gap is CBP asking about their colonoscopy must hear the
    # colonoscopy rule, not the blood-pressure one.
    measure_id = measures.detect_measure(message) or callback_context.state.get(
        "selected_measure_id"
    )
    try:
        gate = _build_gate_response(verdict, member_id, measure_id)
    except Exception as exc:  # noqa: BLE001
        # Fail closed. If we cannot look up the rule we still refuse -- we just
        # refuse without quoting one. Never fall through to the model.
        logger.warning("compliance gate lookup failed: %s", exc)
        gate = {
            "category": verdict["category"],
            "refusal": (
                "I can't answer that one. Decisions about coverage or your "
                "medical care aren't mine to make."
            ),
            "route": ADVOCATE_ROUTE
            if verdict["category"] == COVERAGE_DETERMINATION
            else CLINICIAN_ROUTE,
        }

    if invocation_id:
        callback_context.state["_gated_invocation"] = invocation_id

    # Surface it to the UI without a second round trip.
    callback_context.state["last_gate"] = {
        "gated": True,
        "category": gate["category"],
        "confidence": verdict["confidence"],
        "signals": verdict["signals"],
        "compliance_flag_id": gate.get("compliance_flag_id"),
        "general_rule": (gate.get("general_rule") or {}).get("statement"),
        "route_to": gate["route"]["route_to"],
    }

    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=_refusal_text(gate))]
        )
    )


# --------------------------------------------------------------------------
# Caller authorization (Release of Information)
# --------------------------------------------------------------------------


def _norm_name(name: Any) -> str:
    """Compare names ignoring case, punctuation, and honorifics: the dataset has
    'Mr. Miguel Gonzalez' where a caller would say 'Miguel Gonzalez'."""
    text = str(name or "").strip().lower().replace(".", "").replace(",", "")
    parts = [p for p in text.split() if p not in {"mr", "mrs", "ms", "miss", "dr"}]
    return " ".join(parts)


def check_caller_authorization(
    caller_name: str, member_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Check whether a caller who is not the member may receive their information.

    Call this whenever someone identifies as a spouse, child, parent, or
    caregiver speaking on the member's behalf. Nothing about the member may be
    disclosed to a third party until this returns authorized=true.

    Args:
        caller_name: The name the caller gave, as they said it.
        member_id: The member identifier, for example MBR00030.

    Returns:
        authorized=true with the relationship on file, or authorized=false with
        the reason and what the caller must do.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        rows = [
            r
            for r in bq.fetch_table("roi_authorizations")
            if r.get("member_id") == member_id
        ]
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    wanted = _norm_name(caller_name)
    if not wanted:
        return common.error_response(
            "CALLER_NAME_REQUIRED", "Ask the caller for their full name."
        )

    match = next(
        (r for r in rows if _norm_name(r.get("authorized_caller_name")) == wanted), None
    )

    if match is None:
        flag = log_compliance_flag(
            flag_type="unauthorized_caller_disclosure_blocked",
            severity="high",
            entity_type="member",
            entity_id=member_id,
            description=(
                "A caller not listed on the member's release of information "
                "requested member details. Disclosure was blocked."
            ),
            recommended_action=(
                "Verify identity through Member Services and obtain a signed "
                "authorization before any disclosure."
            ),
        )
        tool_context.state["caller_authorized"] = False
        return {
            "status": "ok",
            "authorized": False,
            "reason": "not_on_file",
            "refusal": (
                "I'm not able to share anything about this member's health "
                "information with you. There's no authorization on file for that "
                "name."
            ),
            "route": {
                "route_to": "Member Services",
                "why": (
                    "They can verify identity and add an authorization with the "
                    "member's consent."
                ),
            },
            "compliance_flag_id": flag["flag_id"],
            "instructions_to_agent": (
                "Do not disclose anything: not the member's gaps, not their "
                "appointments, not even whether this member exists. Say the "
                "refusal, route to Member Services, and offer to speak with the "
                "member directly."
            ),
        }

    if match.get("auth_expired") or not match.get("auth_on_file"):
        flag = log_compliance_flag(
            flag_type="expired_roi_disclosure_blocked",
            severity="medium",
            entity_type="member",
            entity_id=member_id,
            description=(
                f"Caller is on file as {match.get('relationship')} but the "
                f"authorization expired {match.get('expiration_date')}. "
                f"Disclosure blocked."
            ),
            recommended_action="Renew the release of information with the member.",
        )
        tool_context.state["caller_authorized"] = False
        return {
            "status": "ok",
            "authorized": False,
            "reason": "expired",
            "relationship": match.get("relationship"),
            "expiration_date": match.get("expiration_date"),
            "refusal": (
                f"You're listed as the member's {str(match.get('relationship')).lower()}, "
                f"but that authorization expired on {match.get('expiration_date')}, so "
                f"I can't share their information until it's renewed."
            ),
            "route": {
                "route_to": "Member Services",
                "why": "They can renew the authorization with the member's consent.",
            },
            "compliance_flag_id": flag["flag_id"],
            "instructions_to_agent": (
                "An expired authorization is not an authorization. Do not "
                "disclose. Be warm about it -- this is usually a caregiver "
                "trying to help -- but do not bend."
            ),
        }

    tool_context.state["caller_authorized"] = True
    tool_context.state["caller_relationship"] = match.get("relationship")
    common.write_audit_event(
        "caller_authorized",
        member_id,
        {
            "relationship": match.get("relationship"),
            "expiration_date": match.get("expiration_date"),
            "auth_id": match.get("auth_id"),
        },
    )
    return {
        "status": "ok",
        "authorized": True,
        "relationship": match.get("relationship"),
        "expiration_date": match.get("expiration_date"),
        "source": f"roi_authorizations ({bq.backend()})",
        "instructions_to_agent": (
            f"This caller is authorized as the member's "
            f"{str(match.get('relationship')).lower()} until "
            f"{match.get('expiration_date')}. You may proceed normally."
        ),
    }
