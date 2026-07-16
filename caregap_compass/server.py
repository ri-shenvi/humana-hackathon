"""Serve the agent and the demo UI from one origin.

    python -m caregap_compass.server      ->  http://localhost:8000

Wraps ADK's own FastAPI app rather than reimplementing sessions: /run_sse,
/apps/.../sessions and the confirmation round-trip all come from
`get_fast_api_app(web=False)`. We add a small read-only /api surface and mount the
UI on the same app.

Same origin is the point. ADK ships an unconditional CSRF origin check that
403s any cross-origin POST, so a separate dev server would need --allow_origins
and a proxy. Serving the UI from the same app removes CORS from the problem
entirely.

`web=True` would bind ADK's own bundled dev UI instead of ours (its assets dir is
hardcoded), so we pass web=False -- which also frees up "/" for us.

`adk web` still works untouched; this is additive.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from . import (
    bq,
    compliance,
    config,
    feedback,
    impact,
    measures,
    privacy,
    scoring,
    weights,
)

logger = logging.getLogger(__name__)

UI_DIR = config.REPO_ROOT / "ui"

api = APIRouter(prefix="/api", tags=["caregap"])


@api.get("/rank/{member_id}")
def get_ranking(member_id: str) -> dict[str, Any]:
    """The decision, for the decomposition panel.

    Deliberately not read off the agent's event stream. `rank_open_gaps` lives on
    the prioritizer, and ADK's AgentTool consumes a sub-agent's events internally
    -- its tool output never reaches the client. That is fine, because
    `scoring.rank_gaps` is pure and deterministic over the same inputs: calling it
    here returns the identical object the tool returned, and the panel renders
    correctly even if the model is slow, wrong, or mid-sentence.
    """
    try:
        member = bq.get_member(member_id)
        if member is None:
            raise HTTPException(404, f"No member {member_id}")
        result = scoring.rank_gaps(
            member_id,
            bq.get_open_gaps(member_id),
            bq.get_dispositions_for_member(member_id),
            bq.get_interventions(),
        )
    except bq.DataUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    # Enrich each row with the plan's standing on that measure, so the panel can
    # say "1 star, trending down" next to the weight.
    for row in [result.get("selected"), *result.get("rejected", [])]:
        if not row:
            continue
        star_row = bq.get_star_measure(row["measure_id"])
        row["measure_context"] = weights.describe(row["measure_id"], star_row)

    result["member"] = {
        "member_id": member_id,
        "first_name": member.get("first_name"),
        "plan_type": member.get("plan_type"),
        "age": member.get("age"),
    }
    return result


@api.get("/roadmap/{member_id}")
def get_roadmap(member_id: str) -> dict[str, Any]:
    """The member's care year: what's done, what's left, and in what order.

    Closed gaps are real history — `last_service_date` is populated for exactly
    the 254 closed rows and empty for all 298 open ones, so "done on <date>" is
    evidence, not decoration.

    The open ones are ordered by the same ranking the agent uses, so the roadmap
    and the recommendation can never disagree. Both call scoring.rank_gaps.
    """
    try:
        member = bq.get_member(member_id)
        if member is None:
            raise HTTPException(404, f"No member {member_id}")
        gaps = [g for g in bq.get_all_gaps() if g.get("member_id") == member_id]
        ranking = scoring.rank_gaps(
            member_id,
            [g for g in gaps if str(g.get("gap_status", "")).lower() == "open"],
            bq.get_dispositions_for_member(member_id),
            bq.get_interventions(),
        )
    except bq.DataUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    order = {}
    if ranking.get("selected"):
        order[ranking["selected"]["gap_id"]] = (0, ranking["selected"]["score"])
        for i, r in enumerate(ranking.get("rejected", []), start=1):
            order[r["gap_id"]] = (i, r["score"])

    today = config.today()
    steps = []
    for gap in gaps:
        closed = str(gap.get("gap_status", "")).lower() == "closed"
        rank, score = order.get(gap.get("gap_id"), (99, None))
        dtc = scoring.days_to_close(gap.get("due_date"), today)
        steps.append(
            {
                "gap_id": gap.get("gap_id"),
                "measure_id": gap.get("measure_id"),
                "measure_name": gap.get("measure_name"),
                "status": "done" if closed else "open",
                "due_date": gap.get("due_date"),
                "days_to_close": dtc,
                "overdue": (not closed) and dtc is not None and dtc < 0,
                "completed_on": gap.get("last_service_date") or None,
                "outreach_attempts": gap.get("outreach_attempts"),
                "weight": weights.weight_for(gap.get("measure_id", "")),
                "plain_weight": weights.plain_weight(gap.get("measure_id", "")),
                "action": measures.action_for(gap.get("measure_id", "")),
                "priority_rank": None if closed else rank + 1,
                "score": score,
                "is_next": (not closed) and rank == 0,
            }
        )

    # Done first (the member's wins), then the open ones in the agent's own
    # priority order -- not by due date. A roadmap that ordered by deadline would
    # contradict the recommendation sitting next to it.
    steps.sort(key=lambda s: (s["status"] != "done", s.get("priority_rank") or 99))

    done = sum(1 for s in steps if s["status"] == "done")
    return {
        "status": "ok",
        "member_id": member_id,
        "first_name": member.get("first_name"),
        "as_of": today.isoformat(),
        "total": len(steps),
        "done": done,
        "open": len(steps) - done,
        "pct_complete": round(done / len(steps) * 100) if steps else 0,
        "next_step": next((s for s in steps if s["is_next"]), None),
        "steps": steps,
        "source": f"care_gaps ({bq.backend()})",
    }


@api.get("/impact")
def get_impact(measure: str = Query("CBP")) -> dict[str, Any]:
    """The slide number, for the strip across the top."""
    try:
        result = impact.headline(measure)
    except bq.DataUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if result.get("status") != "ok":
        raise HTTPException(404, result.get("error_message", "no impact"))
    return result


@api.get("/compliance-flags")
def get_flags(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Refusals this agent has logged, newest first.

    Member ids are already masked at write time; nothing here needs redacting.
    """
    flags = compliance.read_flags()
    return {"count": len(flags), "flags": list(reversed(flags))[:limit]}


@api.get("/telemetry")
def get_telemetry() -> dict[str, Any]:
    """Which backend is live, and whether the cache is doing anything.

    The backend line matters: under DATA_BACKEND=auto a credential problem
    degrades to CSV silently, and without this you would demo local files while
    believing you were on BigQuery.
    """
    return {
        "backend": bq.backend(),
        "dataset": config.BQ_DATASET,
        "project": config.GOOGLE_CLOUD_PROJECT or None,
        "model": config.MODEL,
        "demo_today": config.today().isoformat(),
        "hero_member_id": config.HERO_MEMBER_ID,
        "cache": bq.cache_stats(),
        "feedback": feedback.summary(),
    }


@api.get("/members")
def list_members(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """Member picker. Names are masked -- the picker does not need to identify
    anyone, it needs to distinguish them."""
    try:
        rows = bq.fetch_table("members")
    except bq.DataUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    open_counts: dict[str, int] = {}
    for gap in bq.get_all_gaps():
        if str(gap.get("gap_status", "")).lower() == "open":
            open_counts[gap["member_id"]] = open_counts.get(gap["member_id"], 0) + 1

    members = [
        {
            "member_id": r["member_id"],
            "label": f"{r['member_id']} · {privacy.mask_value('first_name', r['first_name'])} "
            f"{r.get('plan_type')} · {r.get('age')}",
            "plan_type": r.get("plan_type"),
            "open_gaps": open_counts.get(r["member_id"], 0),
            "is_hero": r["member_id"] == config.HERO_MEMBER_ID,
        }
        for r in rows
    ]
    # Hero first, then members with the most open gaps -- a member with one gap
    # has nothing to rank and makes a poor demo.
    members.sort(key=lambda m: (not m["is_hero"], -m["open_gaps"], m["member_id"]))
    return {"app_name": "caregap_compass", "members": members[:limit]}


def create_app():
    from google.adk.cli.fast_api import get_fast_api_app

    app = get_fast_api_app(
        agents_dir=str(config.REPO_ROOT),
        web=False,
        session_service_uri=f"sqlite:///{config.DATA_ROOT / 'sessions.db'}",
    )
    # Registered after ADK's routes and before the catch-all mount. Starlette
    # matches in registration order, so /run_sse and /apps/... still win over "/".
    app.include_router(api)

    if UI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    else:
        logger.warning("No UI at %s -- serving the API only.", UI_DIR)
    return app


app = create_app()


def main() -> None:
    """Serve the agent, the /api surface and the UI from one process.

    Host and port come from the environment because 127.0.0.1 is right on a
    laptop and wrong everywhere else: it binds loopback only, so on a GCP VM,
    in Cloud Shell, or on Cloud Run the container starts, logs happily, and
    accepts nothing. Cloud Run also injects PORT (8080) and health-checks it.

      local        python -m caregap_compass.server
      Cloud Shell  HOST=0.0.0.0 PORT=8080 python -m caregap_compass.server   (then Web Preview)
      Cloud Run    PORT is injected; set HOST=0.0.0.0
    """
    import os

    import uvicorn

    # PORT is the Cloud Run / Cloud Shell convention and wins if present.
    port = int(os.getenv("PORT") or os.getenv("CARE_GAP_PORT") or 8000)
    # Default to loopback: binding 0.0.0.0 by accident on a shared box exposes
    # member data to the network. Opt in explicitly.
    host = os.getenv("HOST", "127.0.0.1")

    config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print()
    print("  CareGap Compass")
    print(f"    ui        http://{shown}:{port}")
    print(f"    bind      {host}:{port}" + ("" if host != "127.0.0.1" else "   (loopback only — set HOST=0.0.0.0 to expose)"))
    print(f"    backend   {bq.backend()}")
    print(f"    model     {config.MODEL}")
    print(f"    today     {config.today()}  (pinned; the dataset's slots expire otherwise)")
    print()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
