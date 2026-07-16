# Demo script — exact prompts for the recording

**Member:** `MBR00030` (Donna-era DSNP member, 79, hypertensive) ·
**Clock:** 2026-06-10 · **Runtime:** ~4 minutes of screen capture

## Before you hit record

```bash
python -m caregap_compass.scripts.smoke_test   # must print "all checks passed"
rm -f caregap_compass/data/runtime/compliance_flags.jsonl   # clean flag log for the demo
adk web
```

Open `localhost:8000`, select **caregap_compass**. Have a second terminal ready —
you'll `cat` the compliance flag at the end. That beat is worth the extra window.

If the agent drifts, the tools are deterministic — re-run the turn. Everything
below is verified; the only variable is the model's phrasing.

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

**Expect:** he responds 3/4 by phone; two mailers came back undeliverable; CBP
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

Then, second terminal:

```bash
tail -1 caregap_compass/data/runtime/compliance_flags.jsonl
```

Shows `coverage_determination_request_refused`, severity `high`, entity `***030`.

**Narrate:** *"It knows the rule. It refuses to apply it. It routes to a human,
and it leaves evidence — same schema as the compliance_flags table. Coverage
determinations stay with a licensed human."*

> **This is the Round 2 beat.** With TLT, spend your time here.

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
| **"Is it real?"** | Run it live, or `smoke_test` — 5 seconds, no model, proves the whole tool layer. |
| **"Where does 3x come from?"** | CMS Star Ratings methodology: intermediate-outcome measures are 3x, process 1x. The dataset ships no weight column; we derive it and a test fails if the data contradicts it. |
| **"Why only 2 stars?"** | Because 5 stars needs a 74% closure rate and our best campaign hit 41.7%. We'd rather bring you a number that survives contact. |
| **"What if you had no response history?"** | Three-tier fallback, all built and tested. The rejection survives on weight alone. |
| **"Could the LLM get the math wrong?"** | It never does the math. `scoring.py` is pure Python; the model narrates. Recompute any line on screen. |
| **"Does it scale past care gaps?"** | `scoring.py` takes gaps, dispositions, interventions as plain arguments — no ADK, no LLM. Swap the inputs, rank interventions for advocates or actions for providers. |
| **"How far is the provider?"** | The synthetic coordinates don't match the addresses, so we rank relatively and refuse to quote miles rather than invent a distance. |
| **"What about PII?"** | Masked before anything is logged; member IDs scrubbed out of the retrieval index; the model gets a first name and nothing else. |

## Do not say

- ❌ "3.2 miles away" — fabricated. Say "the closest of your in-network options."
- ❌ "Closing 74% gets us to 5 stars" — that's the ceiling, not a claim.
- ❌ Any named litigation. Same point, without litigating our employer.
- ❌ "Diabetic eye exam" / "flu shot" — those measures don't exist in this data.
