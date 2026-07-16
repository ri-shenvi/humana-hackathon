# CareGap Compass — My Next Best Health Action

**Team Cool People · HUM-HAC-113 · STARs: My Next Best Health Action**

A member-facing agent that doesn't list your care gaps — it decides **which one
matters most for you right now**, says **why it rejected the others**, and books
it. Coverage determinations stay with a licensed human.

> The obvious build here is a chatbot that lists your gaps. That's the thing that
> already doesn't work — a mailer is a list. So we built the ranking layer
> instead.

## The decision, on screen

Ask *"what should I do about my health?"* as member `MBR00030` and the agent
shows its arithmetic rather than asserting a pick:

```
CBP    Controlling Blood Pressure         weight 3.0 x urgency 0.85 x propensity 0.26 = 0.655   <- SELECTED
COA    Care for Older Adults - Medication weight 1.0 x urgency 0.79 x propensity 0.40 = 0.312
OMW    Osteoporosis Management in Women   weight 1.0 x urgency 0.80 x propensity 0.23 = 0.182
```

> *Not your osteoporosis follow-up — it's single-weighted. Your blood pressure
> check is triple-weighted, 40 days to close, and you answer the phone 3 times
> out of 4 while two mailers came back undeliverable.*

Note CBP has **lower** propensity than COA. It wins on **weight** — which is the
whole argument, and it survives on real data.

## Quick start

```bash
# 1. install
pip install -r requirements.txt

# 2. configure
cp .env.example .env

# 3. check this machine — auth, BigQuery, tables, clock, parity
python -m caregap_compass.scripts.doctor --model

# 4. run
python -m pytest tests/ -q                        # 151 passed, no credentials needed
python -m caregap_compass.scripts.smoke_test      # every tool, no LLM, ~5s
python -m caregap_compass.scripts.compute_impact  # the number for the slide

python -m caregap_compass.server                  # the demo UI at localhost:8000
adk web                                           # ADK's dev UI, as a fallback
```

Steps 1–2, 4 need **no credentials** — the tests and smoke test run entirely on
the committed CSVs. Only `adk web` needs a model.

**For the model**, on Humana use **Vertex AI + ADC** — not an API key. One
`gcloud auth application-default login` authenticates *both* Gemini and
BigQuery, so `.env` holds no secrets:

```bash
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<the HUM-HAC-113 project id>
GOOGLE_CLOUD_LOCATION=us-central1
```

An AI Studio `GOOGLE_API_KEY` works for local scratch only — it bills a personal
account and is commonly policy-blocked on enterprise projects.

**[`SETUP.md`](SETUP.md) is the full handoff**: auth, BigQuery load and
verification, the demo clock, pending work, and the known-good numbers. Read it
before running this on another machine.

`doctor` is the one command that tells you the truth about any environment — it
prints the exact fix for whatever is red.

## Architecture

```
root orchestrator  (agent.py)
 │
 ├── before_model_callback ── THE COMPLIANCE GATE, enforced in code
 │                            a gated turn never reaches the model at all
 │
 ├── check_caller_authorization   roi_authorizations → may we disclose?
 ├── prioritizer  (AgentTool)     ← the differentiator
 │     rank_open_gaps        weight x urgency x propensity, + rejected_because
 │     get_open_gaps         care_gaps        → open gaps + days-to-close
 │     get_measure_weight    stars_performance → CMS Star weight per measure
 │     get_response_history  campaign_dispositions + historical_interventions
 ├── actioner     (AgentTool)     read-only: explains and finds
 │     explain_gap           coverage_rules + claims + call transcripts
 │     find_provider         providers + appointment_slots + members
 ├── book_or_callback    ⚠ confirmation-gated — simulated, idempotent
 ├── submit_gap_dispute  ⚠ confirmation-gated — files a claim, never closes it
 └── record_feedback     → SQLite
```

**Why the actions hang off the root.** `AgentTool` runs a sub-agent in its own
runner with a throwaway session and returns only its final *text*. A
`require_confirmation` prompt raised inside a sub-agent is emitted into a stream
nobody reads, against a session that is then destroyed — the client can never
answer it and the booking silently no-ops. So anything with a consequence lives
at the root, at the same level as the gate. `tests/test_agent_wiring.py` enforces
this.

Supporting modules: `scoring.py` (pure, LLM-free ranking), `weights.py` (CMS
measure classes), `compliance.py` (gate + ROI + flags), `bq.py` (data door +
cache-aside), `cache.py` (LFU+TTL), `privacy.py` (PII masking), `retrieval.py`
(BM25), `measures.py` (measure→CPT/specialty maps), `impact.py`.

## The UI

`python -m caregap_compass.server` → **localhost:8000**. One FastAPI app wrapping
ADK's own (`get_fast_api_app(web=False)`) plus a read-only `/api` surface, with
`ui/index.html` mounted on the same origin — no build step, no CORS, no proxy.

| Panel | Source |
|---|---|
| **The decision** — score bars, the arithmetic, `rejected_because` | `GET /api/rank/{member}` |
| **Compliance gate** — rule → refusal → route, + live flag log | the SSE stream + `/api/compliance-flags` |
| **Impact strip** | `/api/impact` |
| **Backend + cache chip** | `/api/telemetry` |

**The decision panel is REST-driven, not read off the model's stream.** That's
forced *and* better: ADK's `AgentTool` swallows a sub-agent's tool output, so
`rank_open_gaps`' payload never reaches the client — but `scoring.rank_gaps` is
pure, so calling it directly returns the identical object, and the panel stays
correct even if the model is slow, wrong, or mid-sentence.

The panel, impact strip, telemetry, and member picker **work with no model
credentials at all**. Only the chat needs Gemini.

### Colour

Humana Green `#5BA908` (PMS 369 C) is the brand mark — but it measures **2.87:1
on white**, under the 3:1 floor for fills, so it identifies the product and a
darker step from the same hue, `#3F7505`, carries the data.

The gate is **blue, not red**. Measured: dark red vs dark green is **ΔE 0.5–1.4
under deuteranopia** — the same colour to ~8% of men, in a healthcare product.
`#1D4ED8` vs `#3F7505` measures **ΔE 30.4**. And red would frame the best thing we
built as an error; the gate is the feature. Selected/rejected also carry ✔/✗ and a
SELECTED badge, so identity is never colour-alone.

### In rubric language

| What it is | What it's called |
|---|---|
| Scoring formula | Risk / propensity scoring |
| Orchestrator selecting a sub-agent | Agent routing |
| Compliance gate | Classification + confidence-gated escalation |
| `reason` / `rejected_because` fields | Structured outputs |
| `explain_gap` over transcripts + `coverage_rules` | Grounded retrieval |

## How the score works

**`score = weight × urgency × propensity`** — computed in `scoring.py`, which is
pure Python. The model narrates the arithmetic; it never performs it.

- **weight** — the dataset has *no weight column*, so it's derived from **CMS
  measure class**: intermediate-outcome measures (CBP, CDC-H) are 3x, process
  measures 1x. Published methodology, not a heuristic. `AWC`/`FUH` are 0.5 —
  `is_scored_star_measure = False`, so they move no Star revenue but stay
  rankable.
- **urgency** — `1 − days_to_close / 270`, clamped. 40 days → 0.85.
- **propensity** — `max` over channels of
  `member_response_rate(channel) × measure_closure_rate(measure, channel)`. One
  formula, two answers: how likely it closes, *and* which channel to use.

**Fallback ladder** (decided structurally, not at 9:45am):

| Mode | When | Formula |
|---|---|---|
| `full` | member has response history | weight × urgency × propensity |
| `measure_only` | thin member history | weight × urgency × measure closure |
| `degraded` | no intervention data | weight × urgency × gap age |

The rejection survives all three. You lose *"you answered the phone"*; you keep
*"not your osteoporosis check — it's single-weighted."*

## Data

BigQuery dataset `humana_hackathon`, with an automatic local-CSV fallback.

```bash
# Load BigQuery (needs gcloud + bq — easiest in Cloud Shell):
cd caregap_compass/data/runtime/reference
chmod +x import-bq.sh && ./import-bq.sh ../extracted/structured
```

`DATA_BACKEND=auto` (default) uses BigQuery when reachable and falls back to the
CSVs otherwise — reads are identical, only the source differs. Set `bigquery` to
require it, or `csv` to force local. Every read is cache-aside through an
LFU+TTL cache.

### Things about this data you need to know

| | |
|---|---|
| **`DEMO_TODAY=2026-06-10`** | Slots run `2026-06-11..2026-07-10`, so the real clock expires all 178 of them and drives days-to-close negative. Every date goes through `config.today()`. **Never call `date.today()`.** |
| **No weight column** | Derived from CMS measure class — see above. |
| **`coverage_rules` has no `measure_id`** | Joins on `cpt_code` only; `measures.py` bridges it. Where a rule can't be found, tools say cost is unavailable rather than guessing. |
| **Geography is noise** | `MBR00030` has a Maine address, a California zip, and Oregon coordinates. Nearest of 50 providers is 196 mi; median 1,327. Providers are ranked *relatively* ("2nd closest of your 13 in-network options") and the agent is barred from quoting miles. |
| **Disposition codes are messy** | Mixed `SCREAMING_SNAKE` and CSR prose for the same outcomes. Normalized in `scoring.py`. `DELIVERED` counts as an attempt, **not** a response — it's a carrier receipt, not engagement. |

## Compliance gate

**Enforced as a `before_model_callback`, not a tool.** A tool only runs if the
model chooses to call it — which makes the control advisory, and one distracted
turn or one clever reframing is all it takes for an advisory control to not
exist. Here the classifier runs as code on every turn *before the model is
invoked*, and on a gated turn **the model is never called at all**. The agent
can't be talked into a coverage determination, because on those turns there is no
agent to talk to.

Any coverage-determination request → state the general rule from
`coverage_rules`, **refuse the determination**, route to a licensed advocate, log
a flag. Clinical questions route to a clinician. It **fails closed**: if the rule
lookup itself breaks, it still refuses — it just refuses without quoting a rule.
The instruction block remains as a second layer for phrasings the keywords miss.

```
> Is my colonoscopy covered 100%?

For a DSNP plan, the listed rule for Colonoscopy - Diagnostic (CPT 45378) is:
not listed as a covered benefit for this plan; no cost-share figures apply to an
uncovered service, so I can't quote you an amount.

I can't tell you whether your specific claim will be covered or what you'll owe.
That decision belongs to a licensed advocate, not to me.
```

Flags land in `data/runtime/compliance_flags.jsonl`, member IDs masked, schema
identical to the `compliance_flags` table so `bq load` lifts them straight in.
Written locally on purpose: a control that breaks the demo when it fires is worse
than no control.

### Release of information

A second gate, on the same principle. If a caller identifies as someone other
than the member — a spouse, a daughter, a caregiver —
`check_caller_authorization` checks `roi_authorizations` before anything is
disclosed. Not on file, or expired, means **nothing** is shared: not their gaps,
not their appointments, not even whether the member exists. 43 of the 352
authorizations in the dataset are expired, and an expired release is not a
release.

## Impact

```
python -m caregap_compass.scripts.compute_impact       # CBP
python -m caregap_compass.scripts.compute_impact CDC-H
```

Anchored to a closure rate the plan has **actually achieved** — never an assumed
one. Closing 20 of 27 CBP gaps really does reach 5 stars, but that implies a 74%
closure rate and Humana's best CBP campaign on record closes 41.7%. So 5 stars is
stated as the *ceiling*, and the claim is the floor:

> 27 members carry open CBP gaps — triple-weighted, 1 star at 47.06%. Close all
> 27 and CBP reaches 86.3%: 5 stars. That's the size of the prize. The plan's
> best CBP intervention on record (PCP Panel Engagement, 2025) closes 41.7% via
> Provider. At that rate, targeted outreach to these 27 closes 11 and takes CBP
> to 68.6% — 2 stars, for about $605. That's the floor, not the hope.

## Reusability

The Prioritizer is not a care-gap chatbot — it's a **next-best-action ranking
service**. `scoring.py` takes gaps, dispositions, and interventions as plain
arguments and returns a ranked decision; swap the inputs and it ranks
interventions for advocates or actions for providers. The compliance gate is a
reusable enterprise control for any member-facing AI.

## Scope: explicitly out

- Real scheduling integration → **simulated**
- Write-back to campaign systems → adjacent problem statement
- Voice → web chat only
- Any coverage determination → **by design**

## Tests

```bash
python -m pytest tests/ -q                        # 133 tests, no credentials needed
python -m caregap_compass.scripts.smoke_test      # every tool, no LLM, ~5s
python -m caregap_compass.scripts.doctor --model  # is this machine correct
```

| File | Pins |
|---|---|
| `test_scoring.py` | the numbers spoken on stage. If it fails, the demo script is wrong |
| `test_impact.py` | the honesty — the projection can never outrun a historically achieved closure rate |
| `test_compliance.py` | the gate fires in code, fails closed, and never quotes a price for an uncovered service |
| `test_agent_wiring.py` | the wiring itself — confirmation-gated tools stay on the root |
| `test_actioner.py` | booking revalidates, is idempotent, and never invents a provider |
| `test_retrieval_feedback.py` | no member ID survives into the index, the feedback store, or a log |

`test_agent_wiring.py` exists because the others can't see it: they call the tool
functions directly, bypassing ADK's wrapper, so they pass whether or not a tool
is registered on the right agent. That is exactly how booking got silently broken
once already.

## Docs

- [`SETUP.md`](SETUP.md) — **handoff**: auth, BigQuery, pending work, known-good numbers
- [`docs/testing.md`](docs/testing.md) — **run before presenting**: five layers, cheapest first
- [`docs/proposal-v2.md`](docs/proposal-v2.md) — the proposal, reconciled to the real data
- [`docs/demo-script.md`](docs/demo-script.md) — exact prompts for the recording

## Config

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | — | `TRUE` for Humana. Routes to Vertex; no key needed |
| `GOOGLE_CLOUD_PROJECT` | — | Vertex **and** BigQuery |
| `GOOGLE_CLOUD_LOCATION` | — | e.g. `us-central1` |
| `GOOGLE_API_KEY` | — | Local scratch only. Ignored when Vertex is on |
| `CARE_GAP_MODEL` | `gemini-flash-latest` | Pin a version if Vertex rejects the alias |
| `DATA_BACKEND` | `auto` | `auto` / `bigquery` / `csv`. Use `bigquery` for the demo |
| `BQ_DATASET` | `humana_hackathon` | |
| `DEMO_TODAY` | `2026-06-10` | **Do not change** — every slot expires otherwise |
| `HERO_MEMBER_ID` | `MBR00030` | |
| `URGENCY_HORIZON_DAYS` | `270` | |

---

All data is synthetic, generated for the 2026 Tech Intern Hackathon. Not for
clinical or operational use.
