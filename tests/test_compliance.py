"""The compliance gate — the control that has to hold when the model doesn't.

Two failure directions matter and they are not symmetric. A false escalation
costs a member thirty seconds. A false clearance is an unlicensed entity making
a coverage determination. So the classifier is tested for recall on gated
phrasings and for precision on the demo's own questions, and where it is
uncertain it must escalate.
"""

from __future__ import annotations

import pytest

from caregap_compass import compliance, config


class Ctx:
    def __init__(self, **state):
        self.state = dict(state)


HERO = config.HERO_MEMBER_ID


@pytest.fixture(autouse=True)
def isolate_flag_log(tmp_path, monkeypatch):
    """Never append to the real flag log from a test run."""
    monkeypatch.setattr(config, "COMPLIANCE_FLAG_FILE", tmp_path / "flags.jsonl")
    monkeypatch.setattr(config, "AUDIT_FILE", tmp_path / "audit.jsonl")


# --- classifier: must gate -------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Is my colonoscopy covered 100%?",
        "Is this covered?",
        "Will you pay for this visit?",
        "How much will I pay out of pocket?",
        "What will it cost me?",
        "Has my prior auth been approved?",
        "Why was my claim denied?",
        "Can I appeal this denial?",
        "Am I covered for the eye exam?",
        "Will insurance cover the lab?",
        "Can you guarantee this is fully covered?",
    ],
)
def test_coverage_determinations_are_gated(message):
    verdict = compliance.classify_request(message)
    assert verdict["gated"], message
    assert verdict["category"] == compliance.COVERAGE_DETERMINATION


@pytest.mark.parametrize(
    "message",
    [
        "Should I stop taking my blood pressure medication?",
        "Do I have diabetes?",
        "What dose should I take?",
        "Should I go to the ER?",
        "Can you prescribe something for this?",
        "Is it serious?",
    ],
)
def test_clinical_questions_are_gated(message):
    verdict = compliance.classify_request(message)
    assert verdict["gated"], message
    assert verdict["category"] == compliance.CLINICAL_ADVICE


def test_hedged_determination_still_gates():
    """'In general, will you pay for this?' is a determination request wearing a
    hedge. Educational framing may lower confidence; it may never clear the gate."""
    verdict = compliance.classify_request("In general, will you pay for this?")
    assert verdict["gated"]
    assert verdict["category"] == compliance.COVERAGE_DETERMINATION
    assert verdict["confidence"] >= 0.5


# --- classifier: must NOT gate ---------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "What should I do about my health?",
        "Why not the osteoporosis one?",
        "Book it for Tuesday",
        "Isn't the medication review easier to get done?",
        "Why would you call me?",
        "That was actually helpful",
        "I already had that done in March",
        "What is a copay?",
    ],
)
def test_the_demo_script_is_not_blocked(message):
    """Every beat of docs/demo-script.md that should pass, passes. If one of
    these starts gating, the recording breaks."""
    assert not compliance.classify_request(message)["gated"], message


def test_empty_message_is_not_gated():
    assert not compliance.classify_request("")["gated"]
    assert not compliance.classify_request(None)["gated"]


# --- the refusal -----------------------------------------------------------


def test_determination_states_rule_refuses_and_routes():
    ctx = Ctx(authenticated_member_id=HERO, selected_measure_id="COL")
    result = compliance.check_request("Is my colonoscopy covered 100%?", HERO, ctx)

    assert result["allowed"] is False
    assert result["general_rule"]["statement"]          # 1. state the rule
    assert "advocate" in result["refusal"].lower()      # 2. refuse
    assert result["route"]["route_to"] == "licensed member advocate"  # 3. route
    assert result["compliance_flag_id"]                 # 4. leave evidence


def test_uncovered_service_never_quotes_a_price():
    """coverage_rules zeroes copay/cost_share for uncovered services because the
    rule does not apply — not because it is free. Quoting those zeros would tell
    the member the exact opposite of the truth."""
    ctx = Ctx(authenticated_member_id=HERO, selected_measure_id="COL")
    rule = compliance.check_request("Is this covered?", HERO, ctx)["general_rule"]

    assert rule["covered"] is False
    assert rule["copay"] is None
    assert rule["cost_share_pct"] is None
    assert "$" not in rule["statement"]


def test_covered_service_may_state_the_general_rule():
    ctx = Ctx(authenticated_member_id=HERO, selected_measure_id="CBP")
    rule = compliance.check_request("Is this covered?", HERO, ctx)["general_rule"]
    assert rule["covered"] is True
    assert rule["copay"] == 10.0


def test_measure_with_no_cpt_mapping_says_unknown_not_uncovered():
    """SPC is a pharmacy benefit with no CPT in this rule set. 'No rule found'
    must never be reported as 'not covered'."""
    ctx = Ctx(authenticated_member_id=HERO, selected_measure_id="SPC")
    rule = compliance.check_request("Is this covered?", HERO, ctx)["general_rule"]
    assert rule["rule_found"] is False
    assert rule.get("covered") is None


def test_flags_are_written_and_masked():
    ctx = Ctx(authenticated_member_id=HERO, selected_measure_id="COL")
    compliance.check_request("Is this covered?", HERO, ctx)
    flags = compliance.read_flags()
    assert len(flags) == 1
    flag = flags[0]
    assert flag["flag_type"] == "coverage_determination_request_refused"
    assert flag["severity"] == "high"
    assert flag["entity_id"] == "***030"      # never the raw member id
    assert flag["resolved"] is False
    assert HERO not in str(flag)


def test_flag_schema_matches_the_compliance_flags_table():
    """Schema parity is the whole reason these are written locally: `bq load`
    must be able to lift them into the real table."""
    from caregap_compass import bq

    ctx = Ctx(authenticated_member_id=HERO, selected_measure_id="COL")
    compliance.check_request("Is this covered?", HERO, ctx)
    written = set(compliance.read_flags()[0])
    table = set(bq.fetch_table("compliance_flags")[0])
    assert table <= written, f"flag is missing table columns: {table - written}"


def test_in_scope_requests_write_no_flag():
    ctx = Ctx(authenticated_member_id=HERO)
    result = compliance.check_request("What should I do about my health?", HERO, ctx)
    assert result["allowed"] is True
    assert compliance.read_flags() == []


# --- caller authorization (release of information) --------------------------


def test_authorized_caller_is_allowed():
    ctx = Ctx(authenticated_member_id=HERO)
    result = compliance.check_caller_authorization("Douglas Hart", HERO, ctx)
    assert result["authorized"] is True
    assert result["relationship"] == "Child"
    assert ctx.state["caller_authorized"] is True


def test_honorifics_and_case_do_not_defeat_the_match():
    """The table says 'Mr. Miguel Gonzalez'; a caller says 'miguel gonzalez'."""
    ctx = Ctx(authenticated_member_id=HERO)
    assert compliance.check_caller_authorization("miguel gonzalez", HERO, ctx)["authorized"]


def test_unknown_caller_is_refused_and_flagged():
    ctx = Ctx(authenticated_member_id=HERO)
    result = compliance.check_caller_authorization("Random Stranger", HERO, ctx)
    assert result["authorized"] is False
    assert result["reason"] == "not_on_file"
    assert result["compliance_flag_id"]
    assert ctx.state["caller_authorized"] is False
    flags = compliance.read_flags()
    assert flags[0]["flag_type"] == "unauthorized_caller_disclosure_blocked"
    assert flags[0]["severity"] == "high"


def test_refusal_does_not_leak_that_the_member_exists():
    ctx = Ctx(authenticated_member_id=HERO)
    result = compliance.check_caller_authorization("Random Stranger", HERO, ctx)
    blob = result["refusal"] + result["instructions_to_agent"]
    assert HERO not in blob
    assert "gap" not in result["refusal"].lower()


def test_expired_authorization_is_not_an_authorization():
    """43 rows in roi_authorizations are expired. An expired release is a refusal,
    however sympathetic the caller."""
    from caregap_compass import bq

    expired = next(
        (r for r in bq.fetch_table("roi_authorizations") if r.get("auth_expired")), None
    )
    assert expired is not None, "fixture expectation: some authorizations are expired"

    member = expired["member_id"]
    ctx = Ctx(authenticated_member_id=member)
    result = compliance.check_caller_authorization(
        expired["authorized_caller_name"], member, ctx
    )
    assert result["authorized"] is False
    assert result["reason"] == "expired"
    assert compliance.read_flags()[0]["flag_type"] == "expired_roi_disclosure_blocked"


def test_caller_name_is_required():
    ctx = Ctx(authenticated_member_id=HERO)
    assert (
        compliance.check_caller_authorization("", HERO, ctx)["error_code"]
        == "CALLER_NAME_REQUIRED"
    )


# --- the gate as a framework callback --------------------------------------
#
# A tool only runs if the model chooses to call it, which makes it advisory. The
# callback runs as code before the model is invoked at all, so a gated turn never
# reaches the model and cannot be reframed past it.


class CallbackCtx:
    def __init__(self, invocation_id="inv-1", **state):
        self.invocation_id = invocation_id
        self.state = dict(state)


def _request(*texts, role="user"):
    from google.genai import types

    class _Req:
        contents = [
            types.Content(role=role, parts=[types.Part(text=t)]) for t in texts
        ]

    return _Req()


def test_in_scope_turn_reaches_the_model():
    ctx = CallbackCtx(authenticated_member_id=HERO)
    assert (
        compliance.compliance_gate_callback(
            ctx, _request("What should I do about my health?")
        )
        is None
    )
    assert ctx.state["last_gate"]["gated"] is False


def test_gated_turn_never_reaches_the_model():
    """The whole point: on a gated turn the model is not called, so there is
    nothing to prompt-inject, argue with, or jailbreak."""
    ctx = CallbackCtx(authenticated_member_id=HERO, selected_measure_id="COL")
    response = compliance.compliance_gate_callback(
        ctx, _request("Is my colonoscopy covered 100%?")
    )
    assert response is not None
    text = response.content.parts[0].text
    assert "45378" in text                      # the rule
    assert "advocate" in text.lower()           # the refusal + route
    assert ctx.state["last_gate"]["gated"] is True
    assert ctx.state["last_gate"]["compliance_flag_id"]


def test_gate_fires_even_without_the_model_calling_a_tool():
    """Regression guard for the old design: enforcement must not depend on the
    model remembering to call check_request."""
    ctx = CallbackCtx(authenticated_member_id=HERO)
    assert compliance.compliance_gate_callback(ctx, _request("Will you pay for this?"))
    assert compliance.read_flags()


def test_gate_reads_the_latest_user_message_not_the_first():
    ctx = CallbackCtx(authenticated_member_id=HERO)
    response = compliance.compliance_gate_callback(
        ctx, _request("What should I do about my health?", "Is this covered 100%?")
    )
    assert response is not None


def test_tool_output_is_not_treated_as_member_speech():
    """Function responses come back with role='user'. Classifying our own tool
    output would fire the gate on the word 'covered' in a coverage_rules row."""
    from google.genai import types

    class _Req:
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="explain_gap", response={"covered": True}
                        )
                    )
                ],
            )
        ]

    assert compliance.compliance_gate_callback(CallbackCtx(), _Req()) is None


def test_one_user_turn_logs_exactly_one_flag():
    """The callback fires before every model call, and one turn can trigger
    several. Without a guard a single refusal would flood the log."""
    ctx = CallbackCtx(invocation_id="inv-9", authenticated_member_id=HERO)
    message = _request("Is this covered 100%?")
    compliance.compliance_gate_callback(ctx, message)
    compliance.compliance_gate_callback(ctx, message)
    compliance.compliance_gate_callback(ctx, message)
    assert len(compliance.read_flags()) == 1


def test_a_new_turn_flags_again():
    compliance.compliance_gate_callback(
        CallbackCtx(invocation_id="a", authenticated_member_id=HERO),
        _request("Is this covered?"),
    )
    compliance.compliance_gate_callback(
        CallbackCtx(invocation_id="b", authenticated_member_id=HERO),
        _request("Will you pay for this?"),
    )
    assert len(compliance.read_flags()) == 2


def test_gate_fails_closed_when_the_rule_lookup_breaks(monkeypatch):
    """If we cannot look up the rule we still refuse -- we just refuse without
    quoting one. Falling through to the model would be a silent clearance."""
    monkeypatch.setattr(
        compliance,
        "_build_gate_response",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bigquery down")),
    )
    response = compliance.compliance_gate_callback(
        CallbackCtx(authenticated_member_id=HERO), _request("Is this covered 100%?")
    )
    assert response is not None
    assert "aren't mine to make" in response.content.parts[0].text


def test_empty_request_does_not_gate():
    assert compliance.compliance_gate_callback(CallbackCtx(), _request("")) is None
