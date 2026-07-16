"""Star measure weights.

The stars_performance table ships star ratings and benchmarks but no weight
column, so the 1x/3x weighting the prioritizer needs has to come from somewhere
defensible. It comes from CMS measure class: under the Medicare Advantage Star
Ratings methodology, intermediate-outcome measures are weighted 3x and process
measures 1x. That is published methodology, not a heuristic we invented, which is
what makes the ranking auditable.

AWC and FUH carry is_scored_star_measure = False in this dataset, so they move no
Star revenue. They are weighted 0.5 rather than 0 so a member whose only open gaps
are unscored still gets a recommendation -- they simply always lose to a scored
measure.
"""

from __future__ import annotations

from typing import Any

PROCESS = "process"
INTERMEDIATE_OUTCOME = "intermediate outcome"
UNSCORED = "not a scored Star measure"

MEASURE_WEIGHTS: dict[str, tuple[float, str]] = {
    "CBP": (3.0, INTERMEDIATE_OUTCOME),  # Controlling Blood Pressure
    "CDC-H": (3.0, INTERMEDIATE_OUTCOME),  # Comprehensive Diabetes Care - HbA1c
    "COL": (1.0, PROCESS),  # Colorectal Cancer Screening
    "OMW": (1.0, PROCESS),  # Osteoporosis Management in Women
    "SPC": (1.0, PROCESS),  # Statin Therapy for Cardiovascular Disease
    "TRC": (1.0, PROCESS),  # Transitions of Care
    "MRP": (1.0, PROCESS),  # Medication Reconciliation Post-Discharge
    "COA": (1.0, PROCESS),  # Care for Older Adults - Medication Review
    "AWC": (0.5, UNSCORED),  # Annual Wellness Visit
    "FUH": (0.5, UNSCORED),  # Follow-Up After Hospitalization for Mental Illness
}

DEFAULT_WEIGHT = (1.0, PROCESS)

_PLAIN = {
    3.0: "triple-weighted",
    1.0: "single-weighted",
    0.5: "not a scored Star measure",
}


def weight_for(measure_id: str) -> float:
    return MEASURE_WEIGHTS.get(measure_id, DEFAULT_WEIGHT)[0]


def measure_class(measure_id: str) -> str:
    return MEASURE_WEIGHTS.get(measure_id, DEFAULT_WEIGHT)[1]


def plain_weight(measure_id: str) -> str:
    """How the agent should say the weight out loud."""
    return _PLAIN.get(weight_for(measure_id), f"weighted {weight_for(measure_id)}x")


def weight_reason(measure_id: str) -> str:
    cls = measure_class(measure_id)
    if cls == UNSCORED:
        return "not a scored Star measure - closing it moves no Star rating"
    return f"{cls} measure (CMS {weight_for(measure_id):g}x)"


def describe(measure_id: str, star_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Weight plus the live plan context for this measure.

    star_row is a row from stars_performance. When present, its
    is_scored_star_measure flag is cross-checked against the table above; a
    mismatch means the dataset changed under us and the caller should surface it
    rather than silently trust either source.
    """
    weight = weight_for(measure_id)
    info: dict[str, Any] = {
        "measure_id": measure_id,
        "weight": weight,
        "measure_class": measure_class(measure_id),
        "weight_reason": weight_reason(measure_id),
        "plain_weight": plain_weight(measure_id),
        "weight_source": "CMS Star Ratings measure class (intermediate outcome 3x, process 1x)",
    }
    if not star_row:
        return info

    info.update(
        {
            "measure_name": star_row.get("measure_name"),
            "is_scored_star_measure": star_row.get("is_scored_star_measure"),
            "plan_rate_pct": star_row.get("plan_rate_pct"),
            "current_star_rating": star_row.get("current_star_rating"),
            "at_risk": star_row.get("at_risk"),
            "trending": star_row.get("trending"),
            "gap_count": star_row.get("gap_count"),
            "benchmark_4star_pct": star_row.get("benchmark_4star_pct"),
        }
    )

    scored = star_row.get("is_scored_star_measure")
    expected_scored = measure_class(measure_id) != UNSCORED
    if scored is not None and bool(scored) != expected_scored:
        info["weight_conflict"] = (
            f"stars_performance says is_scored_star_measure={scored} but the CMS "
            f"class table treats {measure_id} as "
            f"{'scored' if expected_scored else 'unscored'}. Weight table needs review."
        )
    return info
