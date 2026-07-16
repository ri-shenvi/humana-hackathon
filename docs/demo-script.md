# Demo script — exact prompts for the recording

**Member:** `MBR00030` (Donna-era DSNP member, 79, hypertensive) ·
**Clock:** 2026-06-10 · **Runtime:** ~4 minutes of screen capture

## Before you hit record

```bash
python -m caregap_compass.scripts.doctor --model   # must be green
python -m caregap_compass.scripts.smoke_test       # "all checks passed"
rm -f caregap_compass/data/runtime/compliance_flags.jsonl   # clean flag log
python -m caregap_compass.server
```

Open **localhost:8000**. No second terminal needed — the decomposition, the
compliance flags, the impact number and the live backend are all on screen.

Two things worth knowing before you record:

- **The decision panel is REST-driven.** It renders from `scoring.rank_gaps`
  directly, so it is correct even if the model stalls or phrases something oddly.
  If a turn drifts, re-send it — the panel doesn't lie.
- **Check the backend chip in the header.** It should say `bigquery`. If it says
  `csv fallback`, you are demoing local files. Set `DATA_BACKEND=bigquery` to turn
  that into a loud failure instead of a quiet one.

`adk web` still works as a fallback if the UI misbehaves.

---

## Beat 1 — the ask

> **What should I do about my health?**

**Expect:** it calls `check_request` (passes), routes to prioritizer, and prints
the decomposition verbatim in a code block:

```
CBP    Controlling Blood Pressure         weight 3.0 x urgency 0.85 x propensity 0.26 = 0.655   <- SELECTED
COA    Care for Older Adults - Medication weight 1.0 x urgency 0.79 x propensity 0.40 = 0.312
OMW    Osteoporosis Management in Women   weight 1.0 x urgency 0.80 x propensity 0.23 = 0.182
```

…then names CBP, and **rejects the other two out loud**.

**Narrate over this:** *"It didn't list three gaps. It ranked them, picked one,
and told him why the other two lost."*

## Beat 2 — the rejection, pressed

> **Why not the osteoporosis one?**

**Expect:** restates the `rejected_because` — single-weighted, process measure,
0.18 against 0.66.

**Narrate:** *"Osteoporosis is a process measure — CMS weights it 1x. Blood
pressure is an intermediate outcome — 3x. Same member, same day: one of these
moves the plan's Star rating and one doesn't."*

## Beat 3 — the counter-intuitive one (the best 20 seconds)

> **Isn't the medication review easier to get done?**

**Expect:** yes — COA has *higher* propensity (0.40 vs 0.26). CBP wins on weight.

**Narrate:** *"This is the whole product. The medication review is the easier
close. The agent still says no — because blood pressure is worth three times as
much. That's the trade a mailer can't make."*

> If you only have time for one follow-up, make it this one.

## Beat 4 — why this channel

> **Why would you call me?**

**Expect:** they respond 3/4 by phone; two mailers came back undeliverable; CBP
historically closes 34% via call center.

**Narrate:** *"Five outreach attempts on this gap. Zero closes. Two of them were
mailers that came back undeliverable. The propensity term is the plan finally
noticing."*

## Beat 5 — the guardrail

> **Is my colonoscopy covered 100%?**

**Expect — all three, in order:**
1. States the general rule: *DSNP, CPT 45378 diagnostic colonoscopy, not listed
   as a covered benefit; no cost-share figure applies to an uncovered service.*
2. **Refuses** the determination.
3. Routes to a licensed advocate.

On screen: a blue **compliance gate** block in the conversation, numbered 1-2-3
(rule → refusal → route), and the flag appears in the **Compliance flags** log at
the bottom right — no terminal required.

**Narrate:** *"It knows the rule. It refuses to apply it. It routes to a human,
and it leaves evidence — same schema as the compliance_flags table."*

> **Why it's blue, if a designer asks:** the gate is the feature, not a failure.
> Red would frame the best thing we built as an error. It's also measured —
> dark red against our green is ΔE 1.4 for a deuteranope; they'd be the same
> colour. In a healthcare product that's not a detail.

**Then the line that lands:** *"And notice where that ran. The gate is a
callback that fires before the model is invoked. On a coverage question the LLM
is never called at all — so it can't be reasoned with, reframed, or
prompt-injected. The agent can't be talked into a coverage determination, because
on that turn there is no agent to talk to."*

> **This is the Round 2 beat.** With TLT, spend your time here.

## Beat 5b — the second gate (optional, strong with TLT)

> **Hi, I'm calling for my mother. My name is Robert Chen.**

**Expect:** nothing is disclosed. Not the gaps, not the appointment — not even
whether the member exists. Routed to Member Services, flag logged.

**Narrate:** *"There's no release of information on file for that name, so the
agent says nothing. 43 of the 352 authorizations in this data are expired, and an
expired release is not a release — however sympathetic the caller."*

To show the happy path instead, use **Douglas Hart** (on file as Child, valid to
2027-10-09).

⚠️ Only run this if you have the time. It's a bonus, not a required beat.

## Beat 6 — the action

> **Okay, book the blood pressure check.**

**Expect:** up to three **distinct** in-network providers with real open slots,
top pick explained, then a confirmation prompt.

> **Yes, book it.**

**Expect:** a receipt with a confirmation ID and a stated *simulated*.

**Narrate:** *"Three real providers, real open slots, in-network, and an
appointment type that actually closes a blood-pressure gap. It re-checks all of
that at booking time rather than trusting the search."*

⚠️ **Do not ask "how far away is it?"** — the geography is synthetic and the agent
will correctly refuse to quote miles. Fine in Q&A, dead air on the recording.

## Beat 6b — "I already did that" (the de-duplication catch)

> **Actually I already had my blood pressure checked in March.**

**Expect:** the agent believes them, says records often lag, asks **where**, then
**roughly when** — one question at a time — then files a reconciliation request
and says plainly **the gap stays open pending review**. It never claims to have
closed it.

**Narrate:** *"It doesn't argue, and it doesn't lie. It files the claim and tells
her the gap stays open until a human reconciles it. And it checked: 34 open gaps
in this dataset already have a paid claim on that measure's own service code. The
care happened; the credit didn't land. The plan is chasing those members for an
appointment they've already had. That's not a care gap, it's a data gap — and the
plan's own STARs report lists campaign de-duplication as a top-three recommended
action."*

> Strong with TLT: it's an operational saving the ranking layer gets for free.

## Beat 7 — the loop

> **That was actually helpful.**

**Expect:** `record_feedback` → stored.

**Narrate:** *"Recorded against the recommendation and the score that produced
it. A measure we keep selecting and members keep declining is a propensity signal
the model doesn't have yet."*

## Beat 8 — impact (screen 2, not the chat)

```bash
python -m caregap_compass.scripts.compute_impact
```

**Say the sentence:**

> *"27 members carry open blood-pressure gaps — triple-weighted, one star, 47%.
> Close all 27 and it's 5 stars. That's the size of the prize. But the best CBP
> campaign this plan has ever run closed 41.7%. So at a rate we've actually hit,
> targeted outreach to these 27 closes 11 and takes us to 2 stars for about $605.
> That's the floor, not the hope."*

---

## Q&A — the ones they'll ask

| Question | Answer |
|---|---|
| **"Is it real?"** | Run it live, or `smoke_test` — 5 seconds, no model, proves the whole tool layer. 133 tests, no credentials needed. |
| **"Where does 3x come from?"** | CMS Star Ratings methodology: intermediate-outcome measures are 3x, process 1x. The dataset ships no weight column; we derive it and a test fails if the data contradicts it. |
| **"Why only 2 stars?"** | Because 5 stars needs a 74% closure rate and our best campaign hit 41.7%. We'd rather bring you a number that survives contact. |
| **"What if the model ignores the guardrail?"** | It can't reach it. The gate is a pre-model callback — on a gated turn the LLM is never invoked. And it fails closed: if the rule lookup breaks, it still refuses, it just refuses without quoting a rule. |
| **"What about prompt injection?"** | Same answer. You can't jailbreak a model that wasn't called. |
| **"What if you had no response history?"** | Three-tier fallback, all built and tested. The rejection survives on weight alone. |
| **"Could the LLM get the math wrong?"** | It never does the math. `scoring.py` is pure Python; the model narrates. Recompute any line on screen. |
| **"Does it scale past care gaps?"** | `scoring.py` takes gaps, dispositions, interventions as plain arguments — no ADK, no LLM. Swap the inputs, rank interventions for advocates or actions for providers. Both gates are reusable too. |
| **"How far is the provider?"** | The synthetic coordinates don't match the addresses, so we rank relatively and refuse to quote miles rather than invent a distance. |
| **"What about PII?"** | Masked before anything is logged; member IDs scrubbed out of the retrieval index; the model gets a first name and nothing else. Plus the ROI gate for third-party callers. |
| **"Why isn't the segment data used?"** | Median segment has 3 eligible members. "Members like you: 50% compliant" would be one person out of two. We'd rather use nothing than fake precision. |

## Do not say

- ❌ "3.2 miles away" — fabricated. Say "the closest of your in-network options."
- ❌ "Closing 74% gets us to 5 stars" — that's the ceiling, not a claim.
- ❌ Any named litigation. Same point, without litigating our employer.
- ❌ "Diabetic eye exam" / "flu shot" — those measures don't exist in this data.
