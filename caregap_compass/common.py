"""Shared tool plumbing: authorization, idempotency, audit.

Ported from the original single-file agent. The behaviours worth keeping are the
session binding (a tool may never act for a member the session didn't
authenticate), deterministic idempotency keys (a retried booking must not create
a second appointment), and an audit trail that records the action without
recording the conversation.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from google.adk.tools import ToolContext

from . import bq, config, privacy

_AUDIT_LOCK = threading.Lock()


def error_response(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "error_message": message, **extra}


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic id: identical inputs collapse onto the same record, which is
    what makes retries idempotent."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def resolve_member_id(member_id: str | None, tool_context: ToolContext | None) -> str:
    if member_id:
        return member_id
    if tool_context is not None:
        authenticated = tool_context.state.get("authenticated_member_id")
        if authenticated:
            return str(authenticated)
    return config.HERO_MEMBER_ID


def authorize_member(
    member_id: str, tool_context: ToolContext | None
) -> dict[str, Any] | None:
    """Bind the session to one member. Returns an error dict on refusal, None on
    success.

    Unlike the original, any member present in the dataset may authenticate --
    but once a session is bound, it cannot pivot to a different member.
    """
    if not member_id:
        return error_response("MISSING_MEMBER", "A member_id is required.")

    if bq.get_member(member_id) is None:
        return error_response(
            "UNKNOWN_MEMBER", f"No member {member_id} exists in the dataset."
        )

    if tool_context is None:
        return None

    authenticated = tool_context.state.get("authenticated_member_id")
    if authenticated not in (None, member_id):
        return error_response(
            "MEMBER_SESSION_MISMATCH",
            "The requested member does not match the authenticated session.",
        )

    tool_context.state["authenticated_member_id"] = member_id
    return None


def write_audit_event(event_type: str, member_id: str, details: dict[str, Any]) -> None:
    """Append one masked audit record. Never stores conversation text."""
    config.AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": config.now_iso(),
        "event_type": event_type,
        "member_id_masked": privacy.mask_member_id(member_id),
        "details": privacy.anonymize_for_log(details),
        "synthetic": True,
    }
    with _AUDIT_LOCK:
        with config.AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
