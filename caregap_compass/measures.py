"""Clinical mapping tables: measure -> CPT, specialty, visit type.

coverage_rules joins on cpt_code and carries no measure_id, and
appointment_slots carries no measure_id either. Something has to bridge "this
member has an open CBP gap" to "this CPT rule and this appointment type", and
that bridge is clinical judgement, not data. So it lives here, explicitly, where
it can be reviewed -- rather than being improvised inside a prompt.

Where the mapping is imperfect it says so. A tool that cannot map a measure to a
rule must say costs are unavailable and route to an advocate. It must never
guess a copay.
"""

from __future__ import annotations

from typing import Any

# measure_id -> CPT codes present in coverage_rules, best match first.
MEASURE_CPT: dict[str, list[str]] = {
    "CBP": ["99213", "93000"],  # office visit for BP check; ECG
    "CDC-H": ["83036"],  # HbA1c test
    "COL": ["45378"],  # colonoscopy
    "AWC": ["99397", "99396"],  # preventive visit 65+, then 40-64
    "FUH": ["90837"],  # psychotherapy
    "COA": ["99490"],  # chronic care management / medication review
    "MRP": ["99214"],  # post-discharge office visit
    "OMW": ["77067"],  # imaging; see caveat below
    "SPC": [],  # statin adherence is a pharmacy benefit, no CPT in this dataset
}

# Where the mapping is approximate, say why in the member-facing explanation.
MEASURE_CPT_CAVEATS: dict[str, str] = {
    "OMW": (
        "Osteoporosis management is normally evidenced by a bone-density scan "
        "(DEXA, CPT 77080), which is not present in this plan's rule set. The "
        "closest listed imaging code is used for illustration only, so treat any "
        "cost shown for this measure as unverified."
    ),
    "SPC": (
        "Statin therapy is a pharmacy benefit. There is no medical CPT code for "
        "it in this plan's rule set, so medical cost-share does not apply and no "
        "cost can be quoted here."
    ),
    "CBP": (
        "Blood-pressure control is evidenced during an office visit rather than "
        "by a dedicated procedure code, so the office-visit rule is what applies."
    ),
}

MEASURE_SPECIALTIES: dict[str, list[str]] = {
    "CBP": ["Family Medicine", "Internal Medicine", "Cardiology", "Geriatrics", "Nephrology"],
    "CDC-H": ["Endocrinology", "Family Medicine", "Internal Medicine", "Geriatrics"],
    "COL": ["Gastroenterology", "Internal Medicine"],
    "AWC": ["Family Medicine", "Internal Medicine", "Geriatrics"],
    "FUH": ["Psychiatry"],
    "COA": ["Geriatrics", "Internal Medicine", "Family Medicine"],
    "MRP": ["Family Medicine", "Internal Medicine", "Geriatrics"],
    "OMW": ["OB/GYN", "Orthopedics", "Geriatrics", "Family Medicine"],
    "SPC": ["Cardiology", "Family Medicine", "Internal Medicine"],
}

MEASURE_VISIT_TYPES: dict[str, list[str]] = {
    "CBP": [
        "Follow-Up",
        "Chronic Disease Management",
        "Cardiology Consult",
        "EKG Review",
        "Annual Exam",
        "Preventive Visit",
        "Geriatric Assessment",
        "New Patient",
    ],
    "CDC-H": ["Diabetes Management", "Follow-Up", "Chronic Disease Management", "New Patient"],
    "COL": ["Colonoscopy", "GI Consult"],
    "AWC": ["Annual Wellness Visit", "Preventive Visit", "Annual Exam"],
    "FUH": ["Psychiatric Evaluation", "Medication Management"],
    "COA": ["Geriatric Assessment", "Medication Management", "Annual Wellness Visit", "Follow-Up"],
    "MRP": ["Follow-Up", "Medication Management", "Chronic Disease Management"],
    "OMW": ["Follow-Up", "Annual Exam", "Pre-Op Visit"],
    "SPC": ["Follow-Up", "Cardiology Consult", "Chronic Disease Management"],
}

# Plain-language description of what the measure asks the member to do.
MEASURE_ACTION: dict[str, str] = {
    "CBP": "have your blood pressure checked and recorded at an office visit",
    "CDC-H": "have an HbA1c blood test to check your average blood sugar",
    "COL": "complete a colorectal cancer screening",
    "AWC": "attend your annual wellness visit",
    "FUH": "attend a follow-up visit after your hospital stay",
    "COA": "have your medication list reviewed with a clinician",
    "MRP": "have your medications reconciled after your hospital discharge",
    "OMW": "have your bone health assessed after your fracture",
    "SPC": "start or refill the statin your care team prescribed",
    "TRC": "complete your transition-of-care follow-up after your hospital stay",
}


def cpt_codes_for(measure_id: str) -> list[str]:
    return MEASURE_CPT.get(measure_id, [])


def specialties_for(measure_id: str) -> list[str]:
    return MEASURE_SPECIALTIES.get(measure_id, [])


def visit_types_for(measure_id: str) -> list[str]:
    return MEASURE_VISIT_TYPES.get(measure_id, [])


def caveat_for(measure_id: str) -> str | None:
    return MEASURE_CPT_CAVEATS.get(measure_id)


def action_for(measure_id: str) -> str:
    return MEASURE_ACTION.get(measure_id, "complete this care step")


def coverage_rules_for(
    measure_id: str, plan_type: str, rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The plan's rules for the CPT codes this measure maps to.

    Returns [] when the measure has no CPT mapping (SPC) or the plan lists no
    rule for it. Callers must treat empty as "unknown", never as "not covered".
    """
    codes = cpt_codes_for(measure_id)
    if not codes:
        return []
    order = {code: index for index, code in enumerate(codes)}
    matched = [
        r
        for r in rules
        if r.get("plan_type") == plan_type and r.get("cpt_code") in order
    ]
    matched.sort(key=lambda r: order[r["cpt_code"]])
    return matched
