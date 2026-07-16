# Cool People — My Next Best Health Action (v2, reconciled to the data)

**Team:** Cool People · **Environment:** HUM-HAC-113 · **Problem Statement:**
STARs: My Next Best Health Action

> **What changed from v1 and why.** The original proposal was written before the
> dataset was profiled. Its worked example used *Diabetic Eye Exam* and *Flu
> Shot* — **neither measure exists in this data** — and it assumed
> `stars_performance` carried a 1x/3x weight column, **which it does not**. Every
> number below is now computed from the real tables and verified by tests. §17
> lists every correction. Nothing in this document is aspirational: if a number
> is here, `pytest` pins it.

---

## 1. One-liner

A member-facing agent that doesn't list your care gaps — it **decides which one
matters most for you right now, says why it rejected the others, and books it.**
Coverage decisions stay with a licensed human.

**The sentence we want a leader to repeat:**

> *"The agent knows which gap to push, and knows which questions it isn't allowed
> to answer."*

## 2. The problem

Members with open care gaps get generic, blanket outreach. Nobody ranks. The plan
wastes spend on low-weight measures and on channels the member has already
ignored; the member gets noise instead of a next step.

**This is literally true for our hero member.** `MBR00030` has an open
blood-pressure gap with **5 outreach attempts and 0 closes**. Two of those
mailers came back undeliverable. They answer the phone 3 times out of 4. Nobody
looked.

And it gets worse. **34 open gaps in this dataset already have a paid claim on
the measure's own service code** — the care happened, the credit didn't land. The
plan is chasing those members for an appointment they have already had. That is
not a care gap; it is a data gap, and the plan's own STARs report names
"campaign de-duplication" as a top-three recommended action. Our agent checks
`claims` before it tells anyone to book, and offers to file the reconciliation
instead.

## 3. Name the baseline (say this out loud — in both rounds)

> *"The obvious build here is a chatbot that lists your gaps. We think that's the
> thing that already doesn't work — a mailer is a list. So we built the ranking
> layer instead."*

In Round 1 the contrast is visible (four other teams share this problem
statement). In Round 2 it is not — TLT never saw the chatbots we beat. Five
seconds, both rounds, non-negotiable.

## 4. The solution: ranking, not retrieval

**"Next Best" is the entire ask.** The agent scores every open gap on:

- **Star measure weight** — 1x vs 3x. 3x measures move plan revenue.
- **Days to close** — urgency.
- **Member/segment response history** — which channels actually worked.

…and surfaces **one** recommendation with a stated reason and an explicit
`rejected_because` for the rest.

## 5. Score decomposition — the arithmetic on screen

**`score = weight × urgency × propensity`**

Real output, real member, `MBR00030`, as of 2026-06-10:

```
CBP    Controlling Blood Pressure         weight 3.0 x urgency 0.85 x propensity 0.26 = 0.655   <- SELECTED
COA    Care for Older Adults - Medication weight 1.0 x urgency 0.79 x propensity 0.40 = 0.312
OMW    Osteoporosis Management in Women   weight 1.0 x urgency 0.80 x propensity 0.23 = 0.182
```

**CBP wins by 2.1×.** Read the middle line carefully: **COA has higher propensity
than CBP.** COA is the *easier* close. CBP wins because it is worth **three
times** as much. That is the entire argument of this product, and it survives on
real data rather than on a chosen example.

**Why this is the highest-leverage thing we built:** it converts a chat flourish
into an **auditable decision system**. A judge doesn't take our word for it —
they can recompute any line. This is the difference between a demo and something
Humana could run.

### Where the weight comes from

`stars_performance` has star ratings and benchmarks but **no weight column**. So
we derive weight from **CMS measure class** — published Star Ratings methodology:
intermediate-outcome measures are weighted 3x, process measures 1x.

| Measure | Class | Weight |
|---|---|---|
| CBP, CDC-H | intermediate outcome | **3.0** |
| COL, OMW, SPC, TRC, MRP, COA | process | 1.0 |
| AWC, FUH | not scored (`is_scored_star_measure = False`) | 0.5 |

This is a strength, not a patch: the weighting is **citable**, and a test fails
loudly if the dataset ever contradicts the table.

### The other two terms

- **urgency** = `1 − days_to_close / 270`, clamped. 270 days ≈ the actionable
  remainder of the measurement year. CBP is 40 days out → **0.85**.
- **propensity** = `max` over channels of
  `member_response_rate(channel) × measure_closure_rate(measure, channel)`.
  One formula, two answers: how likely the gap closes, **and which channel to
  use**. For `MBR00030` + CBP that's Call Center (they respond 3/4; CBP closes 34%
  there).

## 6. Architecture (Google ADK)

**Orchestrator (root)** — enforces the gates, routes to a specialist, and holds
every action that has a consequence.

### Prioritizer Agent — the differentiator

*Also a reusable next-best-action ranking service.*

| Tool | Reads | Returns |
|---|---|---|
| `rank_open_gaps` | all of the below | selected + rejected + decomposition |
| `get_open_gaps` | `care_gaps` | open gaps + days-to-close |
| `get_measure_weight` | `stars_performance` + CMS class | weight, star rating, trend |
| `get_response_history` | `campaign_dispositions`, `historical_interventions` | channels this member responds to |

### Actioner Agent — read-only

| Tool | Reads | Returns |
|---|---|---|
| `explain_gap` | `coverage_rules`, `claims`, call transcripts | why it matters, what it costs, what's already on file |
| `find_provider` | `providers`, `members`, `appointment_slots` | nearest in-network provider |

### The actions live on the orchestrator

| Tool | Reads | Returns |
|---|---|---|
| `book_or_callback` ⚠ | `appointment_slots` | simulated confirmation, idempotent |
| `submit_gap_dispute` ⚠ | `care_gaps`, `claims` | files a claim; **never closes the gap** |

⚠ = requires explicit member confirmation before it runs.

**This placement is not cosmetic.** ADK's `AgentTool` runs a sub-agent in its own
runner with a throwaway session and returns only its final text. A confirmation
prompt raised *inside* a sub-agent is emitted into a stream nobody reads, against
a session that is then destroyed — the member can never answer it, and the
booking silently does nothing. So anything with a consequence sits at the root,
at the same level as the gates. **A regression test enforces it.** If asked why:
*the orchestrator never delegates an action it would have to confirm.*

### The compliance gate — enforced in code, not by asking the model

Any coverage **determination** request → state the general rule from
`coverage_rules`, **refuse the determination**, route to a human advocate, log to
`compliance_flags`.

The important part is *where* it runs. It is a `before_model_callback`: it fires
as code on every turn, **before the model is invoked at all**. A gated turn never
reaches the LLM.

> **"The agent can't be talked into a coverage determination, because on those
> turns there is no agent to talk to."**

That is the difference between a control and a suggestion. A gate implemented as
a tool only runs if the model *chooses* to call it — one distracted turn, one
clever reframing, and an advisory control silently doesn't exist. This one can't
be skipped, can't be argued with, and can't be prompt-injected around. It **fails
closed**: if the rule lookup itself breaks, it still refuses — it just refuses
without quoting a rule. The instruction block stays as a second layer for
phrasings the keyword list misses.

### The second gate: release of information

If a caller identifies as anyone other than the member — a spouse, a daughter, a
caregiver — `check_caller_authorization` checks `roi_authorizations` **before
anything is disclosed**. Not on file, or expired, means nothing is shared: not
their gaps, not their appointments, not even whether the member exists.

**43 of the 352 authorizations in this dataset are expired.** An expired release
is not a release, however sympathetic the caller. Both outcomes are demonstrable
on real rows.

## 7. Rubric-legible naming

| What we built | What we call it |
|---|---|
| Scoring formula | **Risk / propensity scoring** |
| Orchestrator selecting a sub-agent | **Agent routing** |
| Compliance gate as a pre-model callback | **Classification + confidence-gated escalation** |
| ROI check on `roi_authorizations` | **Identity / disclosure control** |
| `reason` / `rejected_because` fields | **Structured outputs** |
| `explain_gap` over transcripts + `coverage_rules` + `claims` | **Grounded retrieval** |
| Confirmation-gated actions on the root | **Human-in-the-loop** |

Same build. Rubric-legible. Zero additional build cost.

## 8. Business impact — computed, not vibed

**The number:**

> **27 members carry open Controlling Blood Pressure gaps — triple-weighted,
> currently 1 star at 47.06%. Close all 27 and CBP reaches 86.3%: 5 stars.
> That's the size of the prize. The plan's best CBP intervention on record (PCP
> Panel Engagement, 2025) closes 41.7% via provider engagement. At that rate,
> per-member targeted outreach to these 27 closes 11 and takes CBP to 68.6% — 2
> stars — for about $605. That's the floor, not the hope: it's the rate this plan
> has already hit.**

Source: `stars_performance` + `care_gaps` + `historical_interventions`.
Reproduce: `python -m caregap_compass.scripts.compute_impact`.

| Close | Rate | Stars |
|---|---|---|
| 0 | 47.06% | 1 |
| **11 (41%)** | **68.6%** | **2 ← projected at historical closure rate** |
| 15 (56%) | 76.5% | 3 |
| 17 (63%) | 80.4% | 4 |
| 20 (74%) | 86.3% | 5 ← ceiling |

**Why we don't claim the 5-star number.** Closing 20 of 27 does reach 5 stars —
the arithmetic is right. But it assumes a **74% closure rate**, and the best CBP
campaign this plan has ever run closed **41.7%**. Rating 5 requires a *credible*
path. A Stars-literate judge who prices our headline as fantasy discounts the
ranking work too — which is the part that's real. So we say the ceiling as a
ceiling and claim the floor. **If asked in Q&A, this is the answer that wins the
room.**

## 9. Reusability (Round 2 rubric)

The Prioritizer is **not** a care-gap chatbot. It's a **next-best-action ranking
service**: `scoring.py` takes gaps, dispositions, and interventions as plain
arguments — no ADK, no LLM, no care-gap assumptions — and returns a ranked
decision. Swap the inputs and it ranks interventions for advocates, or actions
for providers.

And **two reusable enterprise controls**, both independent of care gaps:

1. **The compliance gate** — classify → state rule → refuse → route → log, as a
   pre-model callback. Drop it onto any member-facing agent at Humana and that
   agent inherits a determination refusal it cannot skip. It is ~200 lines and
   knows nothing about care gaps.
2. **The ROI disclosure check** — any agent that might talk to a caregiver needs
   this, and it reads a table Humana already maintains.

Zero additional build cost. This is framing, and it moves Feasibility /
Reusability / Scalability from 3 to 4–5.

## 10. Scope: explicitly out

- Real scheduling integration → **simulated**
- Write-back to campaign systems → that's the adjacent problem statement
- Voice → web chat only
- Any coverage determination → **by design**

*Say this in Q&A. Stated tradeoffs read as maturity.*

## 11. Fallback — built, not decided under pressure

v1 planned to decide by 9:45 whether the data supported propensity. We built all
three tiers instead, and the agent states which one ran:

| Mode | When | Formula |
|---|---|---|
| `full` | member response history exists | weight × urgency × propensity |
| `measure_only` | thin member history | weight × urgency × measure closure |
| `degraded` | no intervention data at all | weight × urgency × gap age |

The rejection moment survives all three. We'd lose *"you answer the phone"*; we'd
keep *"not your osteoporosis check — it's single-weighted."* Tested.

## 12. Data provenance — say this if asked

- **The clock is pinned to 2026-06-10.** Appointment slots run 2026-06-11 to
  2026-07-10; the real clock expires all 178 and drives days-to-close negative.
  The stars report is dated 2026-06-10 — that's the dataset's "now".
- **Geography is synthetic noise.** `MBR00030` has a Maine address, a California
  zip, and Oregon coordinates. Nearest of 50 providers is 196 mi. **We rank
  providers relatively** ("2nd closest of your 13 in-network options") and the
  agent is barred from quoting mileage. We did not fabricate a "3.2 miles away."
- **All data is synthetic.** Never presented as real records.

## 13. Day 2 — Round 1 (5 min + 3 min Q&A)

*Scored on: Problem Understanding · Technical Ambition · Creativity · Presentation*

| Time | Beat |
|---|---|
| 0:20 | **Name the baseline** — "the obvious build is a chatbot that lists gaps; a mailer is a list" |
| 0:25 | Problem: 5 outreach attempts on one gap, 0 closes, 2 mailers returned undeliverable |
| 0:30 | Ask: *"What should I do about my health?"* |
| **1:00** | **The moment** — agent rejects out loud, decomposition visible: *"Not your osteoporosis follow-up — single-weighted. Your blood pressure check: triple-weighted, 40 days to close, and you answer the phone 3 times out of 4 while two mailers came back undeliverable."* |
| 1:00 | Action — explains plainly, finds in-network provider, books it |
| 0:45 | Guardrail — *"Is my colonoscopy covered 100%?"* → rule, refusal, route to advocate, flag logged |
| 0:40 | Impact number |

## 14. Day 2 — Round 2 (6 min + 2 min Q&A · one intern presents to TLT)

*Different rubric: Solution Impact · Initial AI Approach · User Experience ·
Feasibility / Reusability / Scalability. **Creativity and Technical Ambition are
no longer scored.** Rebalance accordingly.*

| Time | Beat |
|---|---|
| 0:20 | Name the baseline — TLT did not see the four chatbots we beat |
| 1:20 | **Lead with impact.** The number, first. Including why we claim the floor and not the ceiling |
| **1:00** | **Governance as a design principle.** Best asset with this audience — and it is now structural, not aspirational: *"the gate runs before the model. On a coverage question the LLM is never called. The agent can't be talked into a determination, because there's no agent to talk to."* Then the second gate: an unauthorized caller gets nothing — not the gaps, not even whether the member exists. 43 releases in this data are expired, and an expired release is not a release. |
| 0:50 | The rejection moment, compressed — decomposition on screen |
| 0:50 | Action flow — and the de-duplication catch: *34 open gaps already have a paid claim; we stop chasing those members* |
| 0:40 | AI approach in rubric language: routing, propensity scoring, grounded retrieval, confidence-gated escalation, structured outputs, human-in-the-loop |
| 1:00 | Reusability: NBA ranking service + two reusable enterprise controls → enterprise path |

**Presenter:** whoever tells the **impact** story best. Not whoever wrote the most
code.

## 15. Play the recording

Day 2 requires a live **presentation**, not a live **demo**. Live LLM agents fail
in front of judges — latency, cold start, a bad sample. Narrate over the recorded
perfect run.

If a judge asks *"is it real?"* — run it live in Q&A, where failure costs
nothing. `python -m caregap_compass.scripts.smoke_test` also proves the whole
tool layer in 5 seconds without touching the model.

## 16. Language discipline

- Say: *"Coverage determinations stay with a licensed human — the agent explains
  and routes, it never decides."*
- **Do not** name specific litigation in the room. Same point, without sounding
  like we're litigating our employer.
- **Do not** say "3.2 miles away." The geography is synthetic. Say "the closest of
  your in-network options."

## 17. Corrections from v1 — the audit trail

| v1 said | The data says | v2 does |
|---|---|---|
| Demo on *Diabetic Eye Exam* vs *Flu Shot* | Neither measure exists. Real: CBP, CDC-H, COL, OMW, SPC, TRC, MRP, COA (scored) + AWC, FUH (unscored) | Demo on **CBP vs COA vs OMW** |
| Star weight read from `stars_performance` | **No weight column exists** | Derived from **CMS measure class**, cross-checked by test |
| `weight 3.0 × urgency 0.85 × propensity 0.72 = 1.84` | Illustrative only | **Real: 3.0 × 0.85 × 0.26 = 0.655**, 2.1× margin |
| Hero member unspecified | `member-001` didn't exist | **`MBR00030`** — DSNP, 79, CBP 40 days out, 5 attempts / 0 closes |
| *"you booked by SMS last October"* | They replied YES by SMS, but their **strongest** channel is Call Center (3/4) | *"you answer the phone 3 of 4 times; two mailers came back undeliverable"* |
| *"finds provider 3.2 mi in-network"* | Coordinates are random; nearest is 196 mi | **Relative ranking**, mileage never quoted |
| Fallback decided at 9:45 | — | **All three tiers built and tested** |
| Impact: *"closing k% moves X.X → Y.Y"* | Unbounded k is a vibe | **Anchored to 41.7% historical closure**; ceiling labelled as ceiling |
| — | Slots all predate the wall clock | **`DEMO_TODAY=2026-06-10`** pinned |
| — | `coverage_rules` has no `measure_id` | **`measures.py`** bridges measure → CPT; never guesses a copay |
| — | `DELIVERED` is a carrier receipt, not engagement | Counted as an **attempt, not a response** |

### Added after v2 — things the data turned out to support

| What changed | Why | Where |
|---|---|---|
| Gate moved from a **tool** to a **pre-model callback** | A tool only runs if the model chooses to call it. That is a suggestion, not a control. Now a gated turn never reaches the model. | §6 |
| **ROI caller-authorization gate** added | `roi_authorizations` (352 rows, 43 expired) supports a real disclosure control. Strongest possible asset for the Round 2 governance beat. | §6 |
| **`claims` wired in** | 34 open gaps already have a paid claim on the measure's own CPT. Telling those members to book is the exact noise we exist to stop. | §2, §6 |
| Confirmation-gated actions **moved to the root** | Inside an `AgentTool` a confirmation can never be answered — booking silently no-opped. Caught by a wiring test, not by the tool tests. | §6 |
| `segment_performance` **deliberately not used** | Median segment has 3 eligible members. "Members like you: 50%" would be one person out of two. Fake precision would undermine §8, which is real. | §12 |

### Numbers a judge could check

| | |
|---|---|
| Hero ranking | `MBR00030` → **CBP @ 0.655**, 2.1× over COA |
| Urgency at 40 days | **0.85** |
| Impact | 27 open CBP gaps, 1★ @ 47.06% → **11 closes → 2★** at the historical 41.7% |
| Tests | **133 passing**, no credentials required |

`python -m caregap_compass.scripts.smoke_test` proves the whole tool layer in
five seconds without touching the model. That is the answer to *"is it real?"*

---

*Synthetic data, 2026 Tech Intern Hackathon. Not for clinical or operational use.*
