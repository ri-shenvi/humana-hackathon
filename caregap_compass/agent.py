"""CareGap Compass -- root orchestrator.

A member-facing agent that does not list your care gaps. It decides which one
matters most for you right now, says why it rejected the others, and books it.
Coverage determinations stay with a licensed human.

The orchestrator does exactly two things: it routes to a specialist, and it
enforces the compliance gate. It holds no domain logic -- the ranking lives in
prioritizer/scoring, the action in actioner, the refusal in compliance.

    root
     |-- check_request                compliance gate, every turn, first
     |-- check_caller_authorization   release-of-information gate
     |-- prioritizer   (AgentTool)    which gap matters most, and why not others
     |-- actioner      (AgentTool)    explain it, find a provider
     |-- book_or_callback             confirmation-gated action
     |-- submit_gap_dispute           confirmation-gated action
     `-- record_feedback              was this useful

Why the actions hang off the root rather than the actioner: AgentTool runs a
sub-agent in its own Runner with a throwaway session and never yields its events
to the caller. A require_confirmation prompt raised inside a sub-agent is emitted
into a stream nobody reads, against a session that is then destroyed -- the
client can never answer it and the booking silently no-ops. Anything with a
consequence therefore lives here, at the same level as the gate.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.function_tool import FunctionTool

from . import bq, common, compliance, config
from .actioner import actioner_agent, book_or_callback, submit_gap_dispute
from .feedback import record_feedback
from .prioritizer import prioritizer_agent


def start_session(member_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Authenticate the member and load their profile for this conversation.

    Args:
        member_id: The member identifier, for example MBR00030. Defaults to the
            demo member when omitted.

    Returns:
        The member's first name, plan type, and how many gaps are open.
    """
    member_id = common.resolve_member_id(member_id, tool_context)
    denial = common.authorize_member(member_id, tool_context)
    if denial:
        return denial

    try:
        member = bq.get_member(member_id)
        open_gaps = bq.get_open_gaps(member_id)
    except bq.DataUnavailable as exc:
        return common.error_response("DATA_UNAVAILABLE", str(exc))

    tool_context.state["member_first_name"] = member.get("first_name")
    return {
        "status": "ok",
        "member_id": member_id,
        # The only PII handed to the model: enough to be human, not enough to leak.
        "first_name": member.get("first_name"),
        "plan_type": member.get("plan_type"),
        "language_preference": member.get("language_preference"),
        "open_gap_count": len(open_gaps),
        "as_of": config.today().isoformat(),
        "data_backend": bq.backend(),
    }


INSTRUCTION = f"""
You are CareGap Compass, a member-facing assistant for a Humana Medicare
Advantage plan. You help one member decide what single health action to take
next, and then help them take it.

The default member for this demo is {config.HERO_MEMBER_ID}. Today is
{config.today().isoformat()}. All data here is synthetic.

# Coverage and clinical questions

A compliance gate runs in code before you ever see a turn. If a member asks you
to decide coverage or to give medical advice, that turn never reaches you: the
refusal is issued without you. You do not have to police it and you cannot
override it.

What that leaves you: never volunteer a coverage decision or medical advice on a
turn that does reach you. You may state a general plan rule as general
information. You may never say whether THIS member's claim will be covered, never
quote what they will owe, and never diagnose, interpret a result, or advise on
medication. If a question edges that way, say plainly that decisions about
coverage stay with a licensed advocate and offer to route them.

You can also call check_request yourself if you are unsure about a message.

# Who am I talking to

If anyone identifies as someone other than the member -- a spouse, a daughter, a
son, a caregiver, "I'm calling for my mother" -- call check_caller_authorization
with the name they gave BEFORE you say anything about the member.

- authorized=true  -> proceed normally.
- authorized=false -> disclose NOTHING. Not their gaps, not their appointments,
  not even whether this member exists. Say the refusal, route where it says, and
  offer to speak with the member directly. Be kind about it; this is usually a
  family member trying to help. Do not bend.

# Routing

- "What should I do about my health?", "what matters most?", "why that one?",
  "why not the other one?"            -> prioritizer
- "tell me more", "what does it cost", "find someone"  -> actioner
- "that helped" / "that's not useful" -> record_feedback
- Call start_session once at the start to get the member's name.

One specialist per turn. Pass the member's actual question through; do not
narrow it into something easier.

# Acting -- you do this yourself, never the actioner

You hold the two tools that change something, because both need the member's
explicit confirmation:

- book_or_callback: books the appointment the actioner recommended, or requests
  a callback. Use the exact provider_id and slot_id the actioner returned. Never
  invent them, and never book a slot the member has not agreed to.
- submit_gap_dispute: when the member says they already had this care. Believe
  them and say records often lag. Ask where, then roughly when -- one question at
  a time -- then file it. The gap stays OPEN pending review; say so. Never imply
  you closed it.

Before either: repeat the exact action back in plain language and wait. After:
give the id from the receipt. If no receipt came back, nothing happened -- say
that. Scheduling is simulated; say so.

# How you talk

- Sixth-grade reading level. Short sentences. Say "blood pressure check", not
  "CBP measure compliance".
- One question at a time.
- Lead with the recommendation. No preamble.
- When the prioritizer returns decomposition_text, print it verbatim in a code
  block. Never retype or round those numbers.
- Always say why the other gaps were rejected. That is the entire product.

# Hard rules

- Every fact you state comes from a tool. If no tool gave it to you, you do not
  know it.
- Never invent a gap, provider, appointment, weight, or cost.
- Never diagnose, interpret a result, or advise on medication.
- Never say whether a specific claim is covered. Never quote what the member will
  owe. General rules only, then route.
- Never say an action succeeded without a receipt. No receipt, no claim.
- Scheduling is simulated. Say so.
- Do not quote distances in miles: the coordinates in this dataset are synthetic.
  Say "the closest of your in-network options".
- If a tool errors, say what failed in plain language. Do not paper over it.
- Never present synthetic data as a real medical record.
""".strip()


root_agent = Agent(
    name="caregap_compass",
    model=config.MODEL,
    description=(
        "Decides which of a member's open care gaps matters most right now, "
        "explains why it rejected the others, and books the action -- while "
        "refusing coverage determinations and routing them to a licensed human."
    ),
    instruction=INSTRUCTION,
    # The gate is enforced here, in code, before the model is called -- not by
    # asking the model to remember to call a tool. A control that depends on the
    # model choosing to invoke it is advisory, and one distracted turn or one
    # clever reframing is all it takes for an advisory control to not exist. On a
    # gated turn the model is never called at all, so it cannot be talked into a
    # coverage determination.
    before_model_callback=compliance.compliance_gate_callback,
    tools=[
        compliance.check_request,
        compliance.check_caller_authorization,
        start_session,
        AgentTool(prioritizer_agent),
        AgentTool(actioner_agent),
        # Confirmation-gated. Must be registered here, not inside a sub-agent --
        # see the module docstring.
        FunctionTool(book_or_callback, require_confirmation=True),
        FunctionTool(submit_gap_dispute, require_confirmation=True),
        record_feedback,
    ],
)
