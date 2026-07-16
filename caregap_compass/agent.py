from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.function_tool import FunctionTool


# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------

MODEL = os.getenv("CARE_GAP_MODEL", "gemini-flash-latest")
MEMBER_ID = "member-001"

LOCK = threading.Lock()

BOOKINGS: dict[str, dict[str, Any]] = {}
CALLBACKS: dict[str, dict[str, Any]] = {}
DISPUTES: dict[str, dict[str, Any]] = {}

AUDIT_FILE = Path(__file__).parent / "runtime" / "audit.jsonl"


# ---------------------------------------------------------------------------
# Synthetic date helpers
# ---------------------------------------------------------------------------

def next_day(weekday: int, extra_weeks: int = 0) -> date:
    """
    Return the next occurrence of a weekday.

    Monday is 0 and Sunday is 6.
    """
    today = date.today()
    days_until = (weekday - today.weekday()) % 7

    if days_until == 0:
        days_until = 7

    return today + timedelta(days=days_until + (7 * extra_weeks))


def at(day: date, hour: int, minute: int = 0) -> str:
    """
    Produce an ISO timestamp for synthetic Louisville appointments.

    UTC-04:00 is used for the hackathon demonstration.
    """
    local_timezone = timezone(timedelta(hours=-4))

    return datetime.combine(
        day,
        time(hour, minute),
        local_timezone,
    ).isoformat()


NEXT_SATURDAY = next_day(5)
NEXT_SUNDAY = next_day(6)
SECOND_SATURDAY = next_day(5, extra_weeks=1)
NEXT_TUESDAY = next_day(1)


# ---------------------------------------------------------------------------
# Synthetic member data
# ---------------------------------------------------------------------------

MEMBER = {
    "member_id": MEMBER_ID,
    "first_name": "Jordan",
    "zip_code": "40202",
    "plan_id": "plan-gold-001",
    "preferred_language": "English",
    "max_travel_miles": 15.0,
    "availability_preference": "weekend",
    "accessibility_required": False,
    "synthetic": True,
}


# ---------------------------------------------------------------------------
# Synthetic care-gap source data
#
# Important:
# The model does not calculate or infer these gaps. They are treated as
# authoritative outputs from a synthetic quality-measure source system.
# ---------------------------------------------------------------------------

CARE_GAPS = {
    "gap-a1c-001": {
        "gap_id": "gap-a1c-001",
        "name": "Diabetes A1c monitoring",
        "service_code": "LAB_A1C",
        "status": "open",
        "summary": (
            "A blood test that reflects average blood sugar over the "
            "last two to three months."
        ),
        "evidence": {
            "source_system": "synthetic_quality_measure_engine",
            "source_record_id": "quality-record-883",
            "source_date": "2025-03-12",
            "reason": (
                "No qualifying A1c laboratory result appears in the "
                "synthetic record after March 12, 2025."
            ),
            "confidence": 0.94,
        },
    },
    "gap-mammo-001": {
        "gap_id": "gap-mammo-001",
        "name": "Breast cancer screening",
        "service_code": "MAMMOGRAM",
        "status": "open",
        "summary": (
            "A screening mammogram used to look for breast changes "
            "before symptoms appear."
        ),
        "evidence": {
            "source_system": "synthetic_quality_measure_engine",
            "source_record_id": "quality-record-941",
            "source_date": "2024-01-18",
            "reason": (
                "No qualifying screening mammogram appears in the "
                "synthetic record after January 18, 2024."
            ),
            "confidence": 0.89,
        },
    },
}


# ---------------------------------------------------------------------------
# Approved member-education content
#
# This imitates a governed content repository. The model must use this
# content rather than generating unsupported medical explanations.
# ---------------------------------------------------------------------------

APPROVED_CONTENT = {
    "LAB_A1C": {
        "why_it_matters": (
            "The test helps a care team understand average blood sugar "
            "over about two to three months."
        ),
        "what_to_expect": (
            "A small blood sample is collected at a laboratory or clinic."
        ),
        "preparation": (
            "The test usually does not require fasting, but the member "
            "should follow instructions from the ordering clinician."
        ),
        "source_id": "approved-content-a1c-001",
        "version": "2026.07",
        "reading_level": "plain-language",
    },
    "MAMMOGRAM": {
        "why_it_matters": (
            "Screening can find some breast changes before they can be felt."
        ),
        "what_to_expect": (
            "A technologist positions each breast briefly while X-ray "
            "images are taken."
        ),
        "preparation": (
            "The member should follow the imaging location's instructions "
            "about deodorant, powder, or lotion."
        ),
        "source_id": "approved-content-mammo-001",
        "version": "2026.07",
        "reading_level": "plain-language",
    },
}


# ---------------------------------------------------------------------------
# Synthetic provider and appointment data
# ---------------------------------------------------------------------------

def make_slot(
    slot_id: str,
    service_code: str,
    start: str,
    available: bool = True,
) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "service_code": service_code,
        "start": start,
        "available": available,
    }


def make_provider(
    name: str,
    address: str,
    distance_miles: float,
    networks: list[str],
    services: list[str],
    languages: list[str],
    wheelchair_accessible: bool,
    slots: list[dict[str, Any]],
    active: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "address": address,
        "distance_miles": distance_miles,
        "networks": networks,
        "services": services,
        "languages": languages,
        "wheelchair_accessible": wheelchair_accessible,
        "slots": slots,
        "active": active,
        "directory_verified_at": "2026-07-15T12:00:00Z",
        "synthetic": True,
    }


PROVIDERS = {
    "provider-northside": make_provider(
        name="Northside Diagnostic Center",
        address="1000 Market Street, Louisville, KY",
        distance_miles=5.1,
        networks=["plan-gold-001"],
        services=["LAB_A1C", "MAMMOGRAM"],
        languages=["English", "Spanish"],
        wheelchair_accessible=True,
        slots=[
            make_slot(
                "slot-northside-a1c",
                "LAB_A1C",
                at(NEXT_SATURDAY, 10, 30),
            ),
            make_slot(
                "slot-northside-mammo",
                "MAMMOGRAM",
                at(NEXT_SUNDAY, 11, 0),
            ),
        ],
    ),
    "provider-eastside": make_provider(
        name="Eastside Medical Pavilion",
        address="2200 Shelbyville Road, Louisville, KY",
        distance_miles=8.7,
        networks=["plan-gold-001"],
        services=["LAB_A1C", "MAMMOGRAM"],
        languages=["English"],
        wheelchair_accessible=True,
        slots=[
            make_slot(
                "slot-eastside-a1c",
                "LAB_A1C",
                at(NEXT_SATURDAY, 9, 0),
            ),
            make_slot(
                "slot-eastside-mammo",
                "MAMMOGRAM",
                at(SECOND_SATURDAY, 8, 30),
            ),
        ],
    ),
    "provider-riverside": make_provider(
        name="Riverside Laboratory",
        address="400 River Road, Louisville, KY",
        distance_miles=2.2,
        networks=["plan-gold-001"],
        services=["LAB_A1C"],
        languages=["English", "Arabic"],
        wheelchair_accessible=True,
        slots=[
            make_slot(
                "slot-riverside-a1c",
                "LAB_A1C",
                at(NEXT_TUESDAY, 14, 0),
            ),
        ],
    ),
    "provider-womens-imaging": make_provider(
        name="Women's Imaging Center",
        address="3100 Bardstown Road, Louisville, KY",
        distance_miles=7.9,
        networks=["plan-gold-001"],
        services=["MAMMOGRAM"],
        languages=["English", "Spanish"],
        wheelchair_accessible=False,
        slots=[
            make_slot(
                "slot-womens-mammo",
                "MAMMOGRAM",
                at(NEXT_SATURDAY, 12, 15),
            ),
        ],
    ),
    "provider-out-of-network": make_provider(
        name="QuickCare Laboratory",
        address="10 Main Street, Louisville, KY",
        distance_miles=1.4,
        networks=["plan-silver-002"],
        services=["LAB_A1C"],
        languages=["English"],
        wheelchair_accessible=True,
        slots=[
            make_slot(
                "slot-quickcare-a1c",
                "LAB_A1C",
                at(NEXT_SATURDAY, 8, 0),
            ),
        ],
    ),
    "provider-no-availability": make_provider(
        name="Lakeside Imaging",
        address="800 Lake Avenue, Louisville, KY",
        distance_miles=6.8,
        networks=["plan-gold-001"],
        services=["MAMMOGRAM"],
        languages=["English"],
        wheelchair_accessible=True,
        slots=[
            make_slot(
                "slot-lakeside-mammo",
                "MAMMOGRAM",
                at(NEXT_SATURDAY, 13, 30),
                available=False,
            ),
        ],
    ),
    "provider-wrong-capability": make_provider(
        name="Central Dental Associates",
        address="900 Central Avenue, Louisville, KY",
        distance_miles=3.3,
        networks=["plan-gold-001"],
        services=["DENTAL_CLEANING"],
        languages=["English", "Spanish"],
        wheelchair_accessible=True,
        slots=[
            make_slot(
                "slot-central-dental",
                "DENTAL_CLEANING",
                at(NEXT_SATURDAY, 9, 30),
            ),
        ],
    ),
    "provider-inactive": make_provider(
        name="Former Downtown Laboratory",
        address="55 Downtown Way, Louisville, KY",
        distance_miles=1.9,
        networks=["plan-gold-001"],
        services=["LAB_A1C"],
        languages=["English"],
        wheelchair_accessible=True,
        active=False,
        slots=[
            make_slot(
                "slot-former-lab",
                "LAB_A1C",
                at(NEXT_SATURDAY, 10, 0),
            ),
        ],
    ),
}


# ---------------------------------------------------------------------------
# Shared policy and persistence helpers
# ---------------------------------------------------------------------------

def error_response(
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": code,
        "error_message": message,
    }


def authorize_member(
    member_id: str,
    tool_context: ToolContext,
) -> dict[str, Any] | None:
    """
    Enforce synthetic member authorization and bind the session to one member.
    """
    if member_id != MEMBER_ID:
        return error_response(
            "UNAUTHORIZED_MEMBER",
            "This prototype authorizes only synthetic member member-001.",
        )

    authenticated_member = tool_context.state.get("authenticated_member_id")

    if authenticated_member not in (None, member_id):
        return error_response(
            "MEMBER_SESSION_MISMATCH",
            "The requested member does not match the authenticated session.",
        )

    tool_context.state["authenticated_member_id"] = member_id
    return None


def stable_id(prefix: str, *parts: str) -> str:
    raw_value = "|".join(parts)
    digest = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def write_audit_event(
    event_type: str,
    member_id: str,
    details: dict[str, Any],
) -> None:
    """
    Append a synthetic audit event without storing free-form conversation text.
    """
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "member_id_masked": f"***{member_id[-3:]}",
        "details": details,
        "synthetic": True,
    }

    with LOCK:
        with AUDIT_FILE.open("a", encoding="utf-8") as audit_handle:
            audit_handle.write(json.dumps(event) + "\n")


def find_slot(
    provider: dict[str, Any],
    slot_id: str,
) -> dict[str, Any] | None:
    return next(
        (
            candidate
            for candidate in provider["slots"]
            if candidate["slot_id"] == slot_id
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Gap evidence tools
# ---------------------------------------------------------------------------

def list_open_care_gaps(
    member_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Return the open care gaps supplied by the synthetic source system.

    Args:
        member_id: The authenticated synthetic member ID. Use member-001.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    open_gaps = [
        {
            "gap_id": gap["gap_id"],
            "name": gap["name"],
            "summary": gap["summary"],
            "status": gap["status"],
        }
        for gap in CARE_GAPS.values()
        if gap["status"] == "open"
    ]

    return {
        "status": "success",
        "member": MEMBER,
        "open_gaps": open_gaps,
        "source": "synthetic_quality_measure_engine",
    }


def get_gap_evidence(
    member_id: str,
    gap_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Return evidence explaining why a source system considers a gap open.

    Args:
        member_id: The authenticated synthetic member ID.
        gap_id: The exact care-gap identifier returned by the gap list.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    gap = CARE_GAPS.get(gap_id)

    if not gap:
        return error_response(
            "GAP_NOT_FOUND",
            "The requested care gap was not found.",
        )

    return {
        "status": "success",
        "gap_id": gap_id,
        "gap_name": gap["name"],
        "gap_status": gap["status"],
        "evidence": gap["evidence"],
        "important_context": (
            "The absence of a record does not prove the care was not completed. "
            "Records can be delayed, incomplete, or received from another facility."
        ),
    }


# ---------------------------------------------------------------------------
# Approved education tool
# ---------------------------------------------------------------------------

def get_approved_education(
    member_id: str,
    gap_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Return approved and versioned plain-language education for a care gap.

    Args:
        member_id: The authenticated synthetic member ID.
        gap_id: The exact care-gap identifier.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    gap = CARE_GAPS.get(gap_id)

    if not gap:
        return error_response(
            "GAP_NOT_FOUND",
            "The requested care gap was not found.",
        )

    approved_content = APPROVED_CONTENT.get(gap["service_code"])

    if not approved_content:
        return error_response(
            "APPROVED_CONTENT_NOT_FOUND",
            "Approved education is not available for this care gap.",
        )

    return {
        "status": "success",
        "gap_id": gap_id,
        "gap_name": gap["name"],
        "content": approved_content,
        "disclaimer": (
            "This is general education and is not a diagnosis or "
            "personalized medical advice."
        ),
    }


# ---------------------------------------------------------------------------
# Provider matching tool
# ---------------------------------------------------------------------------

def find_in_network_providers(
    member_id: str,
    gap_id: str,
    tool_context: ToolContext,
    weekend_only: bool = True,
    preferred_language: str | None = None,
    accessible_only: bool = False,
    max_distance_miles: float | None = None,
) -> dict[str, Any]:
    """
    Apply hard filters and deterministic ranking to synthetic providers.

    Hard filters:
    - exact member network
    - required service capability
    - active provider record
    - travel radius
    - available matching appointment
    - optional weekend requirement
    - optional accessibility requirement

    Args:
        member_id: The authenticated synthetic member ID.
        gap_id: The care gap for which the member needs a provider.
        weekend_only: Whether only Saturday and Sunday slots should qualify.
        preferred_language: Preferred spoken language.
        accessible_only: Whether wheelchair accessibility is required.
        max_distance_miles: Maximum acceptable travel distance.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    gap = CARE_GAPS.get(gap_id)

    if not gap or gap["status"] != "open":
        return error_response(
            "GAP_NOT_OPEN",
            "The selected care gap was not found or is not currently open.",
        )

    requested_limit = (
        max_distance_miles
        if max_distance_miles is not None
        else MEMBER["max_travel_miles"]
    )

    effective_distance_limit = min(
        requested_limit,
        MEMBER["max_travel_miles"],
    )

    requested_language = (
        preferred_language or MEMBER["preferred_language"]
    ).casefold()

    recommendations: list[dict[str, Any]] = []

    for provider_id, provider in PROVIDERS.items():
        # Hard filter 1: active directory entry.
        if not provider["active"]:
            continue

        # Hard filter 2: exact network.
        if MEMBER["plan_id"] not in provider["networks"]:
            continue

        # Hard filter 3: correct service capability.
        if gap["service_code"] not in provider["services"]:
            continue

        # Hard filter 4: acceptable travel distance.
        if provider["distance_miles"] > effective_distance_limit:
            continue

        # Hard filter 5: required accessibility.
        if accessible_only and not provider["wheelchair_accessible"]:
            continue

        qualifying_slots = [
            appointment_slot
            for appointment_slot in provider["slots"]
            if (
                appointment_slot["available"]
                and appointment_slot["service_code"] == gap["service_code"]
            )
        ]

        if weekend_only:
            qualifying_slots = [
                appointment_slot
                for appointment_slot in qualifying_slots
                if datetime.fromisoformat(
                    appointment_slot["start"]
                ).weekday() >= 5
            ]

        # Hard filter 6: at least one available matching slot.
        if not qualifying_slots:
            continue

        selected_slot = min(
            qualifying_slots,
            key=lambda appointment_slot: appointment_slot["start"],
        )

        language_match = any(
            language.casefold() == requested_language
            for language in provider["languages"]
        )

        days_until_appointment = max(
            0,
            (
                datetime.fromisoformat(selected_slot["start"]).date()
                - date.today()
            ).days,
        )

        # Explainable deterministic prototype score.
        score = (
            100
            - (provider["distance_miles"] * 2.0)
            - (days_until_appointment * 1.5)
            + (8 if language_match else 0)
            + (5 if provider["wheelchair_accessible"] else 0)
        )

        reasons = [
            f"In network for {MEMBER['plan_id']}",
            f"Offers the service needed for {gap['name']}",
            f"Available at {selected_slot['start']}",
            f"{provider['distance_miles']:.1f} miles from the member",
        ]

        if language_match:
            reasons.append(
                f"Supports the preferred language: "
                f"{preferred_language or MEMBER['preferred_language']}"
            )

        if provider["wheelchair_accessible"]:
            reasons.append("Wheelchair-accessible location")

        recommendations.append(
            {
                "provider_id": provider_id,
                "provider_name": provider["name"],
                "address": provider["address"],
                "distance_miles": provider["distance_miles"],
                "languages": provider["languages"],
                "wheelchair_accessible": provider[
                    "wheelchair_accessible"
                ],
                "network_id": MEMBER["plan_id"],
                "network_verified": True,
                "directory_verified_at": provider[
                    "directory_verified_at"
                ],
                "slot_id": selected_slot["slot_id"],
                "appointment_time": selected_slot["start"],
                "score": round(score, 2),
                "ranking_reasons": reasons,
            }
        )

    recommendations.sort(
        key=lambda recommendation: (
            -recommendation["score"],
            recommendation["appointment_time"],
            recommendation["provider_id"],
        )
    )

    top_recommendations = recommendations[:3]

    tool_context.state["last_provider_results"] = top_recommendations
    tool_context.state["selected_gap_id"] = gap_id

    return {
        "status": "success",
        "gap_id": gap_id,
        "gap_name": gap["name"],
        "filters_applied": {
            "plan_id": MEMBER["plan_id"],
            "service_code": gap["service_code"],
            "weekend_only": weekend_only,
            "preferred_language": (
                preferred_language or MEMBER["preferred_language"]
            ),
            "accessible_only": accessible_only,
            "max_distance_miles": effective_distance_limit,
        },
        "recommendations": top_recommendations,
        "excluded_provider_count": len(PROVIDERS)
        - len(recommendations),
        "important_context": (
            "Network and availability information is synthetic and is "
            "revalidated before simulated booking."
        ),
    }


# ---------------------------------------------------------------------------
# Action tools
# ---------------------------------------------------------------------------

def book_appointment(
    member_id: str,
    gap_id: str,
    provider_id: str,
    slot_id: str,
    tool_context: ToolContext,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """
    Book a synthetic appointment after ADK obtains human confirmation.

    Args:
        member_id: The authenticated synthetic member ID.
        gap_id: The selected open care gap.
        provider_id: Exact provider ID returned by provider search.
        slot_id: Exact available slot ID returned by provider search.
        idempotency_key: Optional stable key for replay protection.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    gap = CARE_GAPS.get(gap_id)

    if not gap or gap["status"] != "open":
        return error_response(
            "GAP_NOT_OPEN",
            "The requested care gap is not currently open.",
        )

    provider = PROVIDERS.get(provider_id)

    if not provider or not provider["active"]:
        return error_response(
            "PROVIDER_INVALID",
            "The selected provider is not active.",
        )

    # Revalidate exact network membership.
    if MEMBER["plan_id"] not in provider["networks"]:
        return error_response(
            "OUT_OF_NETWORK",
            "The selected provider is not in the member's exact network.",
        )

    # Revalidate provider capability.
    if gap["service_code"] not in provider["services"]:
        return error_response(
            "PROVIDER_CAPABILITY_MISMATCH",
            "The selected provider cannot complete this care action.",
        )

    selected_slot = find_slot(provider, slot_id)

    if not selected_slot:
        return error_response(
            "SLOT_NOT_FOUND",
            "The requested appointment slot was not found.",
        )

    if selected_slot["service_code"] != gap["service_code"]:
        return error_response(
            "SLOT_SERVICE_MISMATCH",
            "The appointment slot does not match the selected care gap.",
        )

    effective_idempotency_key = idempotency_key or stable_id(
        "booking-request",
        member_id,
        gap_id,
        provider_id,
        slot_id,
    )

    with LOCK:
        previous_receipt = BOOKINGS.get(effective_idempotency_key)

        if previous_receipt:
            return {
                "status": "success",
                "idempotent_replay": True,
                "receipt": previous_receipt,
            }

        # Revalidate availability immediately before booking.
        if not selected_slot["available"]:
            return error_response(
                "SLOT_UNAVAILABLE",
                "The selected appointment is no longer available.",
            )

        receipt = {
            "confirmation_id": stable_id(
                "appointment-confirmation",
                effective_idempotency_key,
            ),
            "member_id": member_id,
            "gap_id": gap_id,
            "gap_name": gap["name"],
            "provider_id": provider_id,
            "provider_name": provider["name"],
            "provider_address": provider["address"],
            "slot_id": slot_id,
            "appointment_time": selected_slot["start"],
            "status": "booked",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True,
        }

        selected_slot["available"] = False
        BOOKINGS[effective_idempotency_key] = receipt

    write_audit_event(
        event_type="appointment_booked",
        member_id=member_id,
        details={
            "gap_id": gap_id,
            "provider_id": provider_id,
            "slot_id": slot_id,
            "confirmation_id": receipt["confirmation_id"],
        },
    )

    return {
        "status": "success",
        "idempotent_replay": False,
        "receipt": receipt,
        "preparation": APPROVED_CONTENT[
            gap["service_code"]
        ]["preparation"],
    }


def request_callback(
    member_id: str,
    reason: str,
    preferred_window: str,
    tool_context: ToolContext,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """
    Create a synthetic representative callback request after confirmation.

    Args:
        member_id: The authenticated synthetic member ID.
        reason: Concise structured reason for the callback.
        preferred_window: Member's preferred callback window.
        idempotency_key: Optional stable replay-protection key.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    if not reason.strip():
        return error_response(
            "CALLBACK_REASON_REQUIRED",
            "A callback reason is required.",
        )

    if not preferred_window.strip():
        return error_response(
            "CALLBACK_WINDOW_REQUIRED",
            "A preferred callback window is required.",
        )

    effective_idempotency_key = idempotency_key or stable_id(
        "callback-request",
        member_id,
        reason.casefold(),
        preferred_window.casefold(),
    )

    with LOCK:
        previous_receipt = CALLBACKS.get(effective_idempotency_key)

        if previous_receipt:
            return {
                "status": "success",
                "idempotent_replay": True,
                "receipt": previous_receipt,
            }

        receipt = {
            "callback_tracking_id": stable_id(
                "callback",
                effective_idempotency_key,
            ),
            "member_id": member_id,
            "reason": reason,
            "preferred_window": preferred_window,
            "status": "callback_requested",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True,
        }

        CALLBACKS[effective_idempotency_key] = receipt

    write_audit_event(
        event_type="callback_requested",
        member_id=member_id,
        details={
            "callback_tracking_id": receipt["callback_tracking_id"],
            "preferred_window": preferred_window,
        },
    )

    return {
        "status": "success",
        "idempotent_replay": False,
        "receipt": receipt,
    }


def submit_gap_dispute(
    member_id: str,
    gap_id: str,
    facility_name: str,
    approximate_date: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """
    Submit a completed-care reconciliation request.

    This action never closes or modifies the underlying care gap.

    Args:
        member_id: The authenticated synthetic member ID.
        gap_id: The care gap the member believes was already completed.
        facility_name: Facility where the member reports completing the care.
        approximate_date: Approximate completion date supplied by the member.
    """
    authorization_error = authorize_member(member_id, tool_context)

    if authorization_error:
        return authorization_error

    gap = CARE_GAPS.get(gap_id)

    if not gap:
        return error_response(
            "GAP_NOT_FOUND",
            "The requested care gap was not found.",
        )

    if not facility_name.strip():
        return error_response(
            "FACILITY_REQUIRED",
            "The facility name is required.",
        )

    if not approximate_date.strip():
        return error_response(
            "APPROXIMATE_DATE_REQUIRED",
            "An approximate completion date is required.",
        )

    dispute_key = stable_id(
        "gap-dispute",
        member_id,
        gap_id,
        facility_name.casefold(),
        approximate_date.casefold(),
    )

    with LOCK:
        previous_receipt = DISPUTES.get(dispute_key)

        if previous_receipt:
            return {
                "status": "success",
                "idempotent_replay": True,
                "receipt": previous_receipt,
            }

        receipt = {
            "tracking_id": stable_id(
                "reconciliation",
                dispute_key,
            ),
            "member_id": member_id,
            "gap_id": gap_id,
            "gap_name": gap["name"],
            "facility_name": facility_name,
            "approximate_date": approximate_date,
            "status": "submitted_for_review",
            "underlying_gap_changed": False,
            "underlying_gap_status": gap["status"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True,
        }

        DISPUTES[dispute_key] = receipt

    write_audit_event(
        event_type="gap_dispute_submitted",
        member_id=member_id,
        details={
            "gap_id": gap_id,
            "tracking_id": receipt["tracking_id"],
        },
    )

    return {
        "status": "success",
        "idempotent_replay": False,
        "receipt": receipt,
        "important_context": (
            "The underlying care gap remains open while the reconciliation "
            "request is reviewed."
        ),
    }


# ---------------------------------------------------------------------------
# Specialist ADK agents
# ---------------------------------------------------------------------------

gap_evidence_agent = Agent(
    model=MODEL,
    name="gap_evidence_specialist",
    description=(
        "Retrieves authoritative synthetic open care gaps and the evidence "
        "supporting their current status."
    ),
    instruction=f"""
You are the care-gap evidence specialist for synthetic member {MEMBER_ID}.

Rules:
1. Use the available tools for every care-gap fact.
2. Never infer or invent a care gap.
3. Never claim missing data proves the care was not completed.
4. Include the exact gap_id in your response.
5. Explain source dates and confidence in plain language.
6. Distinguish source-system facts from uncertainty.
""",
    tools=[
        list_open_care_gaps,
        get_gap_evidence,
    ],
)


member_education_agent = Agent(
    model=MODEL,
    name="member_education_specialist",
    description=(
        "Retrieves governed, versioned, plain-language educational content "
        "for a selected care gap."
    ),
    instruction=f"""
You are the member education specialist for synthetic member {MEMBER_ID}.

Rules:
1. Retrieve education using the approved-content tool.
2. Do not generate unsupported clinical claims.
3. Do not diagnose, prescribe, or provide personalized treatment advice.
4. Use clear, non-technical language.
5. Include the approved source_id and content version.
6. State that the information is general education.
""",
    tools=[
        get_approved_education,
    ],
)


provider_match_agent = Agent(
    model=MODEL,
    name="provider_match_specialist",
    description=(
        "Deterministically filters and ranks synthetic in-network providers "
        "and available appointment slots."
    ),
    instruction=f"""
You are the provider-match specialist for synthetic member {MEMBER_ID}.

Rules:
1. Always call find_in_network_providers.
2. Never invent providers, network status, distances, or appointments.
3. Recommend only providers returned by the tool.
4. Clearly provide each provider_id and slot_id so the orchestrator can act.
5. Explain why the highest-ranked provider was selected.
6. State that all provider information is synthetic.
7. Present no more than three choices.
""",
    tools=[
        find_in_network_providers,
    ],
)


# ---------------------------------------------------------------------------
# Root ADK orchestrator
# ---------------------------------------------------------------------------

root_agent = Agent(
    model=MODEL,
    name="caregap_compass",
    description=(
        "Explains open care gaps, finds valid in-network providers, "
        "and completes simulated next actions."
    ),
    instruction=f"""
# Identity

You are CareGap Compass, a healthcare navigation agent for the synthetic
member Jordan, whose member ID is {MEMBER_ID}.

This is a hackathon demonstration using entirely synthetic data.

# Delegation

Use the specialist agents as follows:

- Open care gaps or evidence:
  call gap_evidence_specialist.

- Why care matters, what to expect, or preparation:
  call member_education_specialist.

- Provider, network, distance, accessibility, language, or availability:
  call provider_match_specialist.

# Actions

You can:

- book a simulated appointment,
- create a representative callback request,
- submit a completed-care reconciliation request.

Before initiating an action:

1. Identify all required values.
2. Repeat the exact proposed action in plain language.
3. Tell the member that confirmation is required.
4. Call the appropriate action tool.
5. Do not claim completion until the tool returns a success receipt.

For appointments, use only an exact provider_id and slot_id returned by
provider_match_specialist.

For a completed-care claim:

1. Ask where it was completed.
2. Ask approximately when it was completed.
3. Repeat the proposed reconciliation request.
4. Submit the request only through submit_gap_dispute.
5. Clearly state that the care gap remains open pending review.

# Safety rules

- Never invent care gaps.
- Never invent clinical evidence.
- Never invent providers or appointments.
- Never claim a provider is in network without the provider tool.
- Never diagnose a condition.
- Never prescribe treatment.
- Never promise coverage or member cost.
- Never silently alter a care-gap record.
- Never say an appointment or request succeeded without a tool receipt.
- Explain uncertainty plainly.
- Ask only one necessary follow-up question at a time.
- Use short paragraphs and plain language.
- Present at most three provider choices.
""",
    tools=[
        AgentTool(agent=gap_evidence_agent),
        AgentTool(agent=member_education_agent),
        AgentTool(agent=provider_match_agent),
        FunctionTool(
            book_appointment,
            require_confirmation=True,
        ),
        FunctionTool(
            request_callback,
            require_confirmation=True,
        ),
        FunctionTool(
            submit_gap_dispute,
            require_confirmation=True,
        ),
    ],
)
