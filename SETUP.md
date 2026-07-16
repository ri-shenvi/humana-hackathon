# SETUP — running CareGap Compass on the Humana environment

Handoff doc. Everything the next machine (or the next agent) needs to take this
from a fresh clone to a working demo, plus what is still unfinished.

**Read this top to bottom once before running anything.** Two decisions here are
not defaults — the auth mode and the demo clock — and getting either wrong makes
the app look broken in ways that are hard to diagnose.

**One command tells you the truth about any machine:**

```bash
python -m caregap_compass.scripts.doctor --model
```

It checks auth, ADC, a live model call, BigQuery, every table's row count,
CSV↔BigQuery parity, the demo clock, and repo hygiene — and prints the exact fix
for anything red. Run it first. Run it again after every change below.

---

## 1. Auth — use Vertex AI, not an API key

**Do not put a `GOOGLE_API_KEY` in this repo on a Humana machine.** AI Studio keys
are consumer credentials: they bill a personal account rather than the hackathon
project, and enterprise policy commonly blocks them.

Use **Vertex AI + Application Default Credentials**. The reason this is the good
path and not just the compliant one: **the same ADC authenticates both Gemini and
BigQuery**. One login, both services, and `.env` ends up holding *zero secrets* —
so there is nothing in this repo that could leak.

### `.env`

```bash
cp .env.example .env
```

Then set exactly these:

| Variable | Value | Notes |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | routes google-genai to Vertex; **this is the switch** |
| `GOOGLE_CLOUD_PROJECT` | *the HUM-HAC-113 project id* | **the one value you must look up** |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | |
| `CARE_GAP_MODEL` | `gemini-flash-latest` | if Vertex rejects the alias, pin `gemini-2.5-flash` |
| `DATA_BACKEND` | `bigquery` | for the demo — see §3 |
| `DEMO_TODAY` | `2026-06-10` | **do not change** — see §4 |
| `HERO_MEMBER_ID` | `MBR00030` | |

Leave `GOOGLE_API_KEY` commented out. If it is set *and* Vertex is on, Vertex
wins and the key is silently ignored — confusing, and a needless secret on disk.

### Credentials

```bash
gcloud auth application-default login
```

On a GCP VM or Cloud Shell, ADC is usually already present — skip the login and
just confirm with the doctor. If using a service account, it needs:

| Role | For |
|---|---|
| `roles/aiplatform.user` | Gemini via Vertex |
| `roles/bigquery.dataViewer` | reading `humana_hackathon` |
| `roles/bigquery.jobUser` | running queries |

**Verify:** `python -m caregap_compass.scripts.doctor --model` → auth mode should
say *Vertex AI (correct for an enterprise project)* and the live model call
should pass. That call is the first real proof the model works; nothing else in
this repo exercises it.

---

## 2. Install

```bash
pip install -r requirements.txt
```

`google-adk==2.3.0`, `google-cloud-bigquery`, `python-dotenv`, `pytest`. FastAPI
and uvicorn come in with ADK. Python 3.11+.

**Verify:** `python -m pytest tests/ -q` → **133 passed**. This needs no
credentials at all — it runs entirely on the local CSVs. If this fails on a
fresh clone, stop and fix it before touching auth.

---

## 3. BigQuery

The dataset is `humana_hackathon`, 12 tables. The CSVs are committed under
`caregap_compass/data/runtime/extracted/structured/`, so the app works with no
BigQuery at all — but the demo should be on BigQuery.

### Load it

`import-bq.sh` needs the `bq` CLI, which is **not** on a stock laptop. Easiest in
**Google Cloud Shell**:

```bash
cd caregap_compass/data/runtime/reference
chmod +x import-bq.sh
./import-bq.sh ../extracted/structured
```

That creates the dataset and loads all 12 CSVs with `--autodetect --replace`.

### Verify it took

```bash
python -m caregap_compass.scripts.doctor
```

Expect every table green at its exact row count:

| Table | Rows | | Table | Rows |
|---|---|---|---|---|
| members | 200 | | campaign_dispositions | 568 |
| care_gaps | 552 | | compliance_flags | 312 |
| claims | 880 | | coverage_rules | 80 |
| providers | 50 | | roi_authorizations | 352 |
| appointment_slots | 178 | | historical_interventions | 27 |
| segment_performance | 169 | | stars_performance | 10 |

A wrong count means a partial load — re-run `import-bq.sh`. The doctor also runs
a **parity check** (CSV vs BigQuery row counts) and re-scores the hero member on
the live backend: it must still select **CBP @ 0.655**. If BigQuery disagrees
with the CSVs, the number on the slide is wrong.

### Set the backend deliberately

| `DATA_BACKEND` | Behaviour | Use |
|---|---|---|
| `auto` | BigQuery if reachable, else CSV | local dev |
| `bigquery` | require BigQuery, fail loudly | **the demo** |
| `csv` | always local | offline / no GCP |

Use `bigquery` for the demo. Under `auto`, a credential problem degrades to CSV
**silently** — the demo still works and you would never know you weren't hitting
BigQuery. `bigquery` turns that into an error you can see.

`bq.py` is the only module that touches data, so nothing else changes.

---

## 4. The demo clock — do not skip this

`DEMO_TODAY=2026-06-10`.

The synthetic data was generated around June 2026: **every appointment slot is
dated 2026-06-11 → 2026-07-10**. On the real wall clock all 178 are in the past,
so booking finds nothing and days-to-close goes negative — scoring and booking
both break, quietly. The stars report is dated 2026-06-10; that is the dataset's
"now".

Every date in the codebase routes through `config.today()`. **Never call
`date.today()`.** There is a check for this:

```bash
grep -rn "date\.today()\|datetime\.now()" caregap_compass/ --include=*.py | grep -v config.py
# must return nothing
```

The doctor reports bookable slots; expect **158 of 178**.

---

## 5. Run it

```bash
python -m caregap_compass.scripts.doctor --model      # 1. is this machine sane
python -m pytest tests/ -q                            # 2. 133 passed
python -m caregap_compass.scripts.smoke_test          # 3. every tool, no LLM, ~5s
python -m caregap_compass.scripts.compute_impact      # 4. the slide number
adk web                                               # 5. chat at :8000
```

`smoke_test` needs no credentials — it drives the tools directly. If it passes
and the chat still misbehaves, the problem is the prompt, not the plumbing. It is
also the answer to *"is it real?"* in Q&A: 5 seconds, no model, whole tool layer.

---

## 6. Pending work

Ordered by what blocks what.

| # | Item | State | Notes |
|---|---|---|---|
| 1 | **No LLM turn has ever run** | ❌ **blocking** | There were never model credentials on the dev machine. Every tool is proven; **the prompts are not.** Do this first: does it really print the decomposition verbatim? Does the gate hold? |
| 2 | **BigQuery never exercised** | ⚠️ | All development ran on CSV. Types are coerced identically on both paths and the doctor checks parity, but the BigQuery path has never executed. §3. |
| 3 | Frontend | ⬜ not built | Only `adk web`. A same-origin FastAPI + single-file UI is planned: score-decomposition panel, compliance banner, confirmation modal, impact strip, cache telemetry. Plan in `docs/` history; `adk web` works meanwhile. |
| 4 | The recording | ❌ | Proposal §15 — the actual deliverable. `docs/demo-script.md` has the exact prompts. |
| 5 | Model name on Vertex | ⚠️ | `gemini-flash-latest` is an alias; Vertex may want a pinned id. `doctor --model` will say. |

### Known-good numbers — if any of these drift, something broke

| | |
|---|---|
| Hero ranking | `MBR00030` → **CBP @ 0.655**, 2.1× margin over COA |
| Urgency | 40 days to close → **0.85** |
| Impact | 27 open CBP gaps, 1★ @ 47.06%; **11 closes → 2★** at the historical 41.7% rate |
| Tests | **133 passed** |
| Bookable slots | **158 of 178** |

---

## 7. Things that will bite you

- **`adk web` on Windows force-disables `--reload`.** Expect the warning; it's benign.
- **Confirmation is `@experimental`** in ADK 2.3.0 (`FeatureName.TOOL_CONFIRMATION`) but `default_on=True`. Works; emits a one-time warning.
- **Confirmation-gated tools must stay on `root_agent`.** `AgentTool` runs a sub-agent in a throwaway session and returns only text, so a confirmation raised inside one can never be answered and booking silently no-ops. `tests/test_agent_wiring.py` enforces this — if it fails, do not "fix" it by moving the tool.
- **The compliance gate is a `before_model_callback`, not a tool.** On a gated turn the model is never called. Do not convert it back to a tool: a control the model can forget to invoke is advisory.
- **Never commit `.env`**, and never commit `data/runtime/*.jsonl` or `*.db` — those are the audit trail, refusal flags, and member feedback. Both are gitignored; keep it that way.
- **Distances are meaningless.** The hero has a Maine address, a California zip, and Oregon coordinates. Providers are ranked *relatively* and the agent is barred from quoting miles. Don't "fix" this by inventing a distance.

---

## 8. Quick reference

```bash
# diagnose anything
python -m caregap_compass.scripts.doctor --model

# force BigQuery for the demo
DATA_BACKEND=bigquery python -m caregap_compass.scripts.smoke_test

# a different member
HERO_MEMBER_ID=MBR00112 python -m caregap_compass.scripts.smoke_test

# a different measure's impact
python -m caregap_compass.scripts.compute_impact CDC-H
```

| Where | What |
|---|---|
| `caregap_compass/scoring.py` | the ranking. Pure, LLM-free, deterministic |
| `caregap_compass/weights.py` | CMS measure-class weights (3x / 1x) |
| `caregap_compass/compliance.py` | the gate + ROI caller check |
| `caregap_compass/bq.py` | the only module that touches data |
| `caregap_compass/config.py` | env + the demo clock |
| `docs/proposal-v2.md` | the proposal, reconciled to the real data |
| `docs/demo-script.md` | exact prompts for the recording |

All data is synthetic. Not for clinical or operational use.
