"""Agent wiring — the bug class the other tests cannot see.

test_scoring and smoke_test both call the tool functions directly, so they pass
whether or not a tool is registered on the right agent. That is exactly how a
confirmation-gated booking ended up inside an AgentTool sub-agent, where its
confirmation prompt is emitted into a stream nobody reads, against a session that
is immediately destroyed — booking silently no-ops and every test stays green.

These tests assert the wiring itself.
"""

from __future__ import annotations

from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.function_tool import FunctionTool

from caregap_compass.actioner import actioner_agent
from caregap_compass.agent import root_agent
from caregap_compass.prioritizer import prioritizer_agent

SUB_AGENTS = [prioritizer_agent, actioner_agent]


# ADK wraps bare functions into FunctionTools lazily at runtime, so agent.tools
# holds raw functions for those and FunctionTool/AgentTool instances for the
# explicitly-wrapped ones. Both shapes have to be handled or these tests inspect
# half the tools and pass on the other half by accident.
_SENTINEL = object()


def _function_tools(agent) -> list:
    return [t for t in agent.tools if isinstance(t, FunctionTool) or callable(t)]


def _tool_name(tool) -> str:
    name = getattr(tool, "name", None)
    if name:
        return name
    func = getattr(tool, "func", None)
    if func is not None:
        return getattr(func, "__name__", repr(tool))
    return getattr(tool, "__name__", repr(tool))


def _requires_confirmation(tool) -> bool:
    """FunctionTool stores this on the private `_require_confirmation`. Reading
    the public name instead silently returns False for every tool, which makes
    these tests pass while testing nothing — the exact failure mode that let the
    original bug through."""
    flag = getattr(tool, "_require_confirmation", _SENTINEL)
    if flag is _SENTINEL:
        return False  # a bare function was never wrapped, so it cannot be gated
    return bool(flag)


def test_the_confirmation_probe_actually_works():
    """Guard the guard: if ADK renames the attribute, every assertion below
    would quietly pass while checking nothing."""
    probe = FunctionTool(lambda: None, require_confirmation=True)
    assert _requires_confirmation(probe) is True
    assert _requires_confirmation(FunctionTool(lambda: None)) is False


# --- the regression --------------------------------------------------------


def test_confirmation_gated_tools_are_on_the_root():
    """A require_confirmation tool must sit on the agent the client's /run_sse
    stream is actually driving. On root that is the client; inside an AgentTool
    it is a throwaway sub-runner."""
    gated = {_tool_name(t) for t in _function_tools(root_agent) if _requires_confirmation(t)}
    assert gated == {"book_or_callback", "submit_gap_dispute"}


def test_no_sub_agent_holds_a_confirmation_gated_tool():
    """AgentTool.run_async consumes the sub-agent's events internally and returns
    only merged text. _part_to_text returns '' for a functionCall part, so a
    confirmation raised in here reaches the orchestrator as an empty string."""
    for agent in SUB_AGENTS:
        offenders = [
            _tool_name(t) for t in _function_tools(agent) if _requires_confirmation(t)
        ]
        assert not offenders, (
            f"{agent.name} holds confirmation-gated tool(s) {offenders}. "
            f"They must be registered on root_agent — a confirmation raised "
            f"inside an AgentTool can never be answered by the client."
        )


def test_sub_agents_are_read_only():
    """The corollary: sub-agents may look things up, but nothing they do can have
    a consequence, because nothing they do can be confirmed."""
    mutating = {"book_or_callback", "submit_gap_dispute", "record_feedback"}
    for agent in SUB_AGENTS:
        names = {_tool_name(t) for t in agent.tools}
        assert not (names & mutating), f"{agent.name} can mutate: {names & mutating}"


# --- structure -------------------------------------------------------------


def test_compliance_gate_is_enforced_by_a_callback_not_a_tool():
    """The load-bearing assertion. A tool runs only if the model chooses to call
    it, which makes the control advisory. The callback runs as code before the
    model is invoked, so a gated turn cannot reach it."""
    from caregap_compass import compliance

    assert root_agent.before_model_callback is compliance.compliance_gate_callback


def test_root_holds_both_gates():
    names = {_tool_name(t) for t in root_agent.tools}
    assert "check_request" in names, "the compliance gate must run at the root"
    assert "check_caller_authorization" in names, "the ROI gate must run at the root"


def test_no_sub_agent_can_bypass_the_gate():
    """Sub-agents are reached through AgentTool, which means the root's callback
    has already run for this turn. If a sub-agent ever became reachable via
    transfer (sub_agents=), control would leave the root and the gate would stop
    firing -- so assert that has not happened."""
    for agent in SUB_AGENTS:
        assert not getattr(agent, "sub_agents", None), (
            f"{agent.name} has sub_agents; transfer would move control away from "
            f"the root and the compliance gate would no longer run every turn."
        )
    assert not getattr(root_agent, "sub_agents", None), (
        "root has sub_agents: control could transfer away and the gate would stop "
        "running. Specialists must be reached via AgentTool."
    )


def test_root_routes_to_both_specialists():
    routed = {t.agent.name for t in root_agent.tools if isinstance(t, AgentTool)}
    assert routed == {"prioritizer", "actioner"}


def test_prioritizer_owns_the_ranking():
    names = {_tool_name(t) for t in prioritizer_agent.tools}
    assert {
        "rank_open_gaps",
        "get_open_gaps",
        "get_measure_weight",
        "get_response_history",
    } <= names


def test_actioner_explains_and_finds_but_does_not_act():
    names = {_tool_name(t) for t in actioner_agent.tools}
    assert names == {"explain_gap", "find_provider"}


def test_every_agent_has_an_instruction_and_a_model():
    for agent in [root_agent, *SUB_AGENTS]:
        assert agent.instruction and len(agent.instruction) > 200, agent.name
        assert agent.model, agent.name


def test_root_instruction_states_the_hard_rules():
    """These lines are the product. If someone trims them, fail loudly."""
    text = root_agent.instruction.lower()
    for phrase in (
        "check_request",
        "check_caller_authorization",
        "never invent",
        "simulated",
        "verbatim",
    ):
        assert phrase in text, f"root instruction no longer mentions {phrase!r}"
