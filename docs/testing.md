# Testing — before you present

Five layers, cheapest first. Each one only makes sense if the layer under it
passed, so **run them in order and stop at the first red**. That is the whole
point: when something breaks on stage you want to already know *which* layer it
was, not be debugging live.

```
L0  doctor        is this machine correct?            ~10s   no creds needed
L1  pytest        is the logic correct?               ~10s   no creds needed
L2  smoke_test    do all the tools work?              ~5s    no creds needed
L3  curl /api     does the backend serve it?          ~5s    no creds needed
L4  the UI        does it render and behave?          ~2m    chat needs Gemini
L5  dry run       does the demo land?                 ~5m    needs Gemini
```

**L0–L3 need no model credentials at all.** If those are green and the demo still
misbehaves, the problem is the prompt — not the data, the scoring, the API, or
the wiring. That knowledge is worth a lot at 3am.

---

## L0 — Is this machine correct?

```bash
python -m caregap_compass.scripts.doctor --model
```

Checks auth mode, ADC, a live model call, BigQuery reachability, all 12 tables at
their exact row counts, CSV↔BigQuery parity, the demo clock, and repo hygiene.
Every failure prints its own fix.

**Must see:**

| | |
|---|---|
| auth mode | `Vertex AI (correct for an enterprise project)` |
| live model call | `gemini-… replied 'ok'` |
| bigquery reachable | `queries will hit BigQuery` |
| all 12 tables | green, exact counts |
| parity | `row counts agree` + `hero ranking on this backend — CBP @ 0.6555` |
| bookable slots | `158 of 178` |

**If `bigquery reachable` is amber**, you are about to demo local CSVs. Fix it or
know it. See [SETUP.md](../SETUP.md) §3.

---

## L1 — Is the logic correct?

```bash
python -m pytest tests/ -q          # expect: 169 passed
```

No credentials, no network. If this fails on a fresh clone, stop — nothing above
it can work.

What each file protects:

| File | If it goes red |
|---|---|
| `test_scoring.py` | **the demo script is wrong** — these are the numbers said out loud |
| `test_impact.py` | the impact claim outran a closure rate the plan has actually hit |
| `test_compliance.py` | the gate stopped firing, or quoted a price for an uncovered service |
| `test_agent_wiring.py` | **booking is silently broken** — see the note below |
| `test_actioner.py` | booking stopped revalidating, or started inventing providers |
| `test_server.py` | the UI mount shadowed ADK's routes, or panel and chat can disagree |
| `test_retrieval_feedback.py` | a member id leaked into the index, the DB, or a log |

> **Never "fix" `test_agent_wiring.py` by moving a tool.** It asserts that
> confirmation-gated tools live on `root_agent`. Inside an `AgentTool`, ADK runs
> the sub-agent in a throwaway session and returns only text — so a confirmation
> raised there can never be answered and booking silently no-ops, with every
> other test still green. That bug shipped once already.

---

## L2 — Do the tools work?

```bash
python -m caregap_compass.scripts.smoke_test    # expect: all checks passed
```

Drives every tool directly, no LLM. ~5 seconds, deterministic.

**This is also your answer to "is it real?" in Q&A** — it proves the entire tool
layer in five seconds without touching the model, so it cannot fail on latency or
a bad sample.

Spot-check the output:

```
CBP    Controlling Blood Pressure    weight 3.0 x urgency 0.85 x propensity 0.26 = 0.655   <- SELECTED
COA    Care for Older Adults         weight 1.0 x urgency 0.79 x propensity 0.40 = 0.312
OMW    Osteoporosis Management       weight 1.0 x urgency 0.80 x propensity 0.23 = 0.182
```

---

## L3 — Does the backend serve it?

```bash
python -m caregap_compass.server     # leave running
```

| Check | Command | Expect |
|---|---|---|
| ranking | `curl -s localhost:8000/api/rank/MBR00030 \| jq .selected.score` | `0.6555` |
| roadmap | `curl -s localhost:8000/api/roadmap/MBR00030 \| jq .pct_complete` | `25` |
| impact | `curl -s "localhost:8000/api/impact?measure=CBP" \| jq .projection.expected_closes` | `11` |
| backend | `curl -s localhost:8000/api/telemetry \| jq -r .backend` | `bigquery` |
| **ADK not shadowed** | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/list-apps` | `200` |
| UI served | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/` | `200` |

The `/list-apps` one matters: the UI is mounted at `/`, which is a catch-all. If
it ever gets registered before ADK's routes, that 404s and the whole agent API
dies while the page still loads fine.

**Panel and chat must agree.** Both come from `scoring.rank_gaps`. If
`/api/rank`'s score ever differs from the decomposition in the chat, one of them
is fabricating — stop and find out which.

---

## L4 — Does the UI work?

Open **localhost:8000**.

### Works with no credentials

Everything except the chat is REST-driven, so verify it before you worry about
Gemini:

- [ ] **Decision panel** — CBP selected at 0.655, COA and OMW dimmed with a
      `rejected_because` on each. Bars proportional.
- [ ] **Arithmetic visible** — `weight 3.0 × urgency 0.85 × propensity 0.26 = 0.655`
- [ ] **Roadmap** — Donna's care year, 1/4 (25%), MRP done 2023-08-05, then
      CBP → COA → OMW.
- [ ] **Impact strip** — 27 open, 1★ 47.06%, 11 closes → 2★.
- [ ] **Data chip** — `BigQuery · live`. **Amber `Local CSV` means you are not on
      BigQuery.**
- [ ] **Member picker** — hero first, names masked (`D***`).
- [ ] **Rail toggle** (foot of the sidebar) — collapses to icons, survives reload.
- [ ] **History** — past conversations listed; clicking one replays it.

### Needs Gemini

- [ ] **Chat** — ask *"What should I do about my health?"* → decomposition in a
      code block, rejections said out loud.
- [ ] **Gate** — *"Is my colonoscopy covered 100%?"* → blue block, rule → refusal
      → route, and a flag appears in the log.
- [ ] **Booking** — *"book it"* → confirmation modal → **Book it** → receipt with
      a confirmation id.

### The one that proves the architecture

**The gate works with no credentials at all.** Try it:

```bash
SID=$(curl -s -X POST localhost:8000/apps/caregap_compass/users/demo-user/sessions \
  -H "Content-Type: application/json" -d '{"state":{"authenticated_member_id":"MBR00030"}}' \
  | jq -r .id)

curl -s -N -X POST localhost:8000/run_sse -H "Content-Type: application/json" -d "{
  \"app_name\":\"caregap_compass\",\"user_id\":\"demo-user\",\"session_id\":\"$SID\",
  \"new_message\":{\"role\":\"user\",\"parts\":[{\"text\":\"Is my colonoscopy covered 100%?\"}]}}"
```

Even with **no API key**, that returns the full refusal — rule, refusal, route —
because the gate is a `before_model_callback` and the model is never invoked. A
normal question on the same session returns `"No API key was provided"`.

> That contrast **is** the governance argument, demonstrable on any laptop. If a
> judge doubts the guardrail, run these two commands.

---

## L5 — Dry run

```bash
rm -f caregap_compass/data/runtime/compliance_flags.jsonl   # clean log
python -m caregap_compass.server
```

Then walk [demo-script.md](demo-script.md) start to finish, three times. Watch for:

- The decomposition printed **verbatim** in a code block, not re-typed or rounded.
- The rejections said **out loud**, not just shown in the panel.
- No mileage quoted (the geography is synthetic).
- No claim of a booking without a confirmation id on screen.

**If a turn drifts, re-send it.** The panel is REST-driven and stays correct
regardless — only the model's phrasing varies.

---

## When something breaks

| Symptom | Layer | Likely cause |
|---|---|---|
| `No API key was provided` | L0 | Vertex not configured. `doctor --model`. |
| Data chip is amber | L0 | BigQuery unreachable → CSVs. SETUP.md §3. |
| Nothing is bookable | L0 | `DEMO_TODAY` unpinned — every slot is in the past. |
| Table row count wrong | L0 | Partial `import-bq.sh` load. Re-run it. |
| Model rejects the model name | L0 | Vertex wants a pinned id: `CARE_GAP_MODEL=gemini-2.5-flash`. |
| Scores changed | L1 | Something in `scoring.py` moved. **The deck is now wrong.** |
| Booking does nothing | L1 | A confirmation tool moved off `root_agent`. |
| Agent API 404s | L3 | The `/` mount shadowed ADK's routes. |
| Panel ≠ chat | L3 | One of them is fabricating. Do not present until resolved. |
| Chat drifts | L4/L5 | The prompt. Everything below it is proven — re-send the turn. |

## Numbers a judge could check

| | |
|---|---|
| Hero ranking | `MBR00030` → **CBP @ 0.655**, 2.1× over COA |
| Urgency at 40 days | **0.85** |
| Roadmap | **1/4 done (25%)**, next = CBP |
| Impact | 27 open, 1★ @ 47.06% → **11 closes → 2★** at the historical 41.7% |
| Tests | **169 passing**, no credentials |
| Bookable slots | **158 of 178** |

If any of these drift, something broke — find out what before you present.
