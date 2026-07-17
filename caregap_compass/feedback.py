"""Feedback store.

The architecture diagram puts PostgreSQL here, collecting member feedback to
improve the ranking later. SQLite is the same shape at zero setup cost: one
file, no server, identical schema, and swapping the DSN is the only change if it
ever needs to be Postgres.

Member ids are masked before they land. The ranking that produced the feedback is
recorded alongside it, because "the member rejected this recommendation" is only
useful if you know what the recommendation was and why it was made.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from google.adk.tools import ToolContext

from . import common, config, privacy

_DB_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    session_id        TEXT,
    member_id_masked  TEXT NOT NULL,
    gap_id            TEXT,
    measure_id        TEXT,
    recommendation_score REAL,
    scoring_mode      TEXT,
    helpful           INTEGER NOT NULL,
    accepted_action   INTEGER,
    comment           TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_measure ON feedback(measure_id);
"""


def _connect() -> sqlite3.Connection:
    config.FEEDBACK_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.FEEDBACK_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record(
    member_id: str,
    helpful: bool,
    gap_id: str | None = None,
    measure_id: str | None = None,
    comment: str | None = None,
    session_id: str | None = None,
    recommendation_score: float | None = None,
    scoring_mode: str | None = None,
    accepted_action: bool | None = None,
) -> dict[str, Any]:
    with _DB_LOCK:
        conn = _connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO feedback (
                    created_at, session_id, member_id_masked, gap_id, measure_id,
                    recommendation_score, scoring_mode, helpful, accepted_action,
                    comment
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.now_iso(),
                    session_id,
                    privacy.mask_member_id(member_id),
                    gap_id,
                    measure_id,
                    recommendation_score,
                    scoring_mode,
                    1 if helpful else 0,
                    None if accepted_action is None else int(accepted_action),
                    comment,
                ),
            )
            conn.commit()
            return {"feedback_id": cursor.lastrowid}
        finally:
            conn.close()


def acceptance_by_measure() -> list[dict[str, Any]]:
    """Which measures members accept when we recommend them.

    This is the loop the diagram calls "use feedback to improve the AI later":
    a measure the ranking keeps selecting and members keep rejecting is a
    propensity signal the scoring does not yet have.
    """
    with _DB_LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT measure_id,
                       COUNT(*)                          AS responses,
                       SUM(helpful)                      AS helpful_count,
                       ROUND(AVG(helpful) * 100, 1)      AS helpful_pct,
                       ROUND(AVG(recommendation_score), 3) AS avg_score
                FROM feedback
                WHERE measure_id IS NOT NULL
                GROUP BY measure_id
                ORDER BY helpful_pct ASC, responses DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def summary() -> dict[str, Any]:
    with _DB_LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(helpful) AS helpful,
                       SUM(CASE WHEN accepted_action = 1 THEN 1 ELSE 0 END) AS accepted
                FROM feedback
                """
            ).fetchone()
            total = row["total"] or 0
            return {
                "total": total,
                "helpful": row["helpful"] or 0,
                "accepted_action": row["accepted"] or 0,
                "helpful_pct": (
                    round((row["helpful"] or 0) / total * 100, 1) if total else None
                ),
                "db": str(config.FEEDBACK_DB),
            }
        finally:
            conn.close()


def record_feedback(
    helpful: bool,
    member_id: str | None = None,
    comment: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Record whether the member found the recommendation useful.

    Args:
        helpful: True if the member found the recommendation useful, False if not.
        member_id: Optional member identifier. Defaults to the authenticated
            member from session state.
        comment: What the member said, in their own words. Pass an empty string
            if they did not elaborate.

    Returns:
        Confirmation that the feedback was stored.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    ranking = tool_context.state.get("last_ranking") or {}
    stored = record(
        member_id=member_id,
        helpful=helpful,
        gap_id=tool_context.state.get("selected_gap_id"),
        measure_id=ranking.get("measure_id"),
        comment=comment or None,
        recommendation_score=ranking.get("score"),
        scoring_mode=ranking.get("scoring_mode"),
    )
    common.write_audit_event(
        "feedback_recorded",
        member_id,
        {
            "feedback_id": stored["feedback_id"],
            "helpful": helpful,
            "measure_id": ranking.get("measure_id"),
        },
    )
    return {
        "status": "ok",
        "feedback_id": stored["feedback_id"],
        "message": "Thanks -- that's recorded and it will tune future rankings.",
    }
