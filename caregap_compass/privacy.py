"""PII masking.

The members table carries real-shaped PII (name, dob, phone, email, address).
None of it may reach an audit record, a feedback row, or a log line. The agent
addresses the member by first name only, and that name is passed deliberately by
the caller rather than harvested from a logged row.
"""

from __future__ import annotations

from typing import Any


PII_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "name",
        "dob",
        "phone",
        "email",
        "address",
        "npi",
        "authorized_caller_name",
    }
)

# Kept in the clear: coarse enough not to identify, useful for provider matching
# and eligibility explanations.
QUASI_IDENTIFIER_FIELDS = frozenset({"zip", "lat", "lon", "city", "state", "age"})


def mask_member_id(member_id: str | None) -> str:
    """MBR00030 -> ***030. Enough to correlate records, not enough to resolve one."""
    if not member_id:
        return "***"
    return "***" + str(member_id)[-3:]


def mask_value(field: str, value: Any) -> Any:
    if value in (None, ""):
        return value
    if field in ("first_name", "last_name", "name", "authorized_caller_name"):
        text = str(value).strip()
        return (text[0] + "***") if text else "***"
    if field == "email":
        text = str(value)
        return "***@" + text.split("@", 1)[1] if "@" in text else "***"
    if field == "phone":
        digits = [c for c in str(value) if c.isdigit()]
        return "***-" + "".join(digits[-4:]) if len(digits) >= 4 else "***"
    if field == "dob":
        return str(value)[:4] + "-**-**"
    return "***"


def mask_pii(record: dict[str, Any]) -> dict[str, Any]:
    """Copy a record with every PII field masked."""
    masked: dict[str, Any] = {}
    for key, value in record.items():
        if key == "member_id":
            masked[key] = mask_member_id(value)
        elif key in PII_FIELDS:
            masked[key] = mask_value(key, value)
        else:
            masked[key] = value
    return masked


def anonymize_for_log(payload: Any) -> Any:
    """Recursively mask a structure destined for a log or persisted record."""
    if isinstance(payload, dict):
        return {
            key: (
                mask_member_id(value)
                if key == "member_id"
                else mask_value(key, value)
                if key in PII_FIELDS
                else anonymize_for_log(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [anonymize_for_log(item) for item in payload]
    return payload


def member_display_name(member: dict[str, Any]) -> str:
    """The only PII the agent is allowed to speak: the member's first name."""
    return str(member.get("first_name") or "there")
