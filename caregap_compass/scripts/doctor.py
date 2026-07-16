"""Environment doctor: prove the thing works *here* before you trust it.

    python -m caregap_compass.scripts.doctor

Checks auth, the model, BigQuery, and CSV/BigQuery parity, and prints exactly
what to run for whatever is broken. Written for the Humana environment, where
you should be on Vertex AI + Application Default Credentials and there should be
no API key anywhere near the repo.

Exit code is non-zero if anything required failed, so it works as a CI or
pre-demo gate. `--model` additionally spends one real (tiny) model call to prove
Gemini is reachable; everything else is free.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .. import config

OK = "  ok  "
WARN = " warn "
FAIL = " FAIL "

_failures: list[str] = []
_warnings: list[str] = []

# Row counts as shipped. A mismatch means the BigQuery load was partial or the
# CSVs drifted -- either way, numbers on the slide would be wrong.
EXPECTED_ROWS = {
    "members": 200,
    "care_gaps": 552,
    "claims": 880,
    "providers": 50,
    "appointment_slots": 178,
    "campaign_dispositions": 568,
    "compliance_flags": 312,
    "coverage_rules": 80,
    "roi_authorizations": 352,
    "historical_interventions": 27,
    "segment_performance": 169,
    "stars_performance": 10,
}


def check(label: str, state: str, detail: str = "") -> None:
    print(f"[{state}] {label}" + (f"  -- {detail}" if detail else ""))
    if state == FAIL:
        _failures.append(label)
    elif state == WARN:
        _warnings.append(label)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 64 - len(title)))


def fix(*lines: str) -> None:
    for line in lines:
        print(f"         -> {line}")


# ---------------------------------------------------------------------------


def check_auth() -> str:
    """Returns the resolved auth mode: 'vertex' | 'api_key' | 'none'."""
    section("auth")

    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "") or os.getenv(
        "GOOGLE_GENAI_USE_ENTERPRISE", ""
    )
    vertex_on = use_vertex.lower() in ("true", "1")
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION")

    if vertex_on:
        mode = "vertex"
        check("auth mode", OK, "Vertex AI (correct for an enterprise project)")
        check(
            "GOOGLE_CLOUD_PROJECT",
            OK if project else FAIL,
            project or "not set -- Vertex cannot resolve a project",
        )
        if not project:
            fix("export GOOGLE_CLOUD_PROJECT=<the HUM-HAC-113 project id>")
        check(
            "GOOGLE_CLOUD_LOCATION",
            OK if location else WARN,
            location or "not set; defaulting may fail. Try us-central1",
        )
        if api_key:
            check(
                "no stray API key",
                WARN,
                "GOOGLE_API_KEY is set but ignored under Vertex. Remove it to "
                "avoid confusion and accidental commits.",
            )
    elif api_key:
        mode = "api_key"
        check(
            "auth mode",
            WARN,
            "AI Studio API key. Fine locally; NOT for the Humana project -- it "
            "bills a personal account and may be blocked by policy.",
        )
        fix(
            "For Humana, switch to Vertex instead:",
            "GOOGLE_GENAI_USE_VERTEXAI=TRUE",
            "GOOGLE_CLOUD_PROJECT=<project id>",
            "GOOGLE_CLOUD_LOCATION=us-central1",
            "gcloud auth application-default login",
        )
    else:
        mode = "none"
        check("auth mode", FAIL, "no Vertex config and no API key -- the model cannot run")
        fix(
            "Humana / enterprise:  GOOGLE_GENAI_USE_VERTEXAI=TRUE + "
            "GOOGLE_CLOUD_PROJECT + gcloud auth application-default login",
            "Local scratch:        GOOGLE_API_KEY=<aistudio key>",
        )

    # ADC backs both Vertex and BigQuery. One credential, two services.
    try:
        import google.auth

        credentials, adc_project = google.auth.default()
        check(
            "application default credentials",
            OK,
            f"{type(credentials).__name__}"
            + (f", project={adc_project}" if adc_project else ", no project"),
        )
        if project and adc_project and project != adc_project:
            check(
                "project agreement",
                WARN,
                f"GOOGLE_CLOUD_PROJECT={project} but ADC resolves {adc_project}. "
                f"The env var wins for the client; make sure that's intended.",
            )
    except Exception as exc:  # noqa: BLE001
        check("application default credentials", FAIL, f"{type(exc).__name__}: {exc}")
        fix(
            "gcloud auth application-default login",
            "…or on a GCP VM/Cloud Shell, attach a service account with "
            "roles/aiplatform.user + roles/bigquery.dataViewer",
        )

    return mode


def check_model(mode: str, spend_a_call: bool) -> None:
    section("model")
    check("configured model", OK, config.MODEL)

    if not spend_a_call:
        check("live model call", WARN, "skipped -- pass --model to actually try it")
        return
    if mode == "none":
        check("live model call", FAIL, "skipped -- no auth configured")
        return

    try:
        from google import genai

        client = genai.Client()
        response = client.models.generate_content(
            model=config.MODEL, contents="Reply with the single word: ok"
        )
        text = (response.text or "").strip()
        check("live model call", OK, f"{config.MODEL} replied {text[:40]!r}")
    except Exception as exc:  # noqa: BLE001
        check("live model call", FAIL, f"{type(exc).__name__}: {str(exc)[:160]}")
        fix(
            "If the model name is rejected on Vertex, try a pinned id "
            "(e.g. CARE_GAP_MODEL=gemini-2.5-flash).",
            "If it is a 403, the project likely lacks the Vertex AI API or the "
            "caller lacks roles/aiplatform.user.",
        )


def check_bigquery(mode: str) -> bool:
    section("bigquery")
    from .. import bq

    check("requested backend", OK, f"DATA_BACKEND={config.DATA_BACKEND}")
    check("dataset", OK, f"{config.GOOGLE_CLOUD_PROJECT or '(project unset)'}.{config.BQ_DATASET}")

    try:
        from google.cloud import bigquery  # noqa: F401
    except ImportError:
        check("google-cloud-bigquery installed", FAIL, "pip install google-cloud-bigquery")
        return False
    check("google-cloud-bigquery installed", OK)

    resolved = bq.backend()
    if resolved == "bigquery":
        check("bigquery reachable", OK, "queries will hit BigQuery")
    else:
        state = FAIL if config.DATA_BACKEND == "bigquery" else WARN
        check(
            "bigquery reachable",
            state,
            "unreachable -- reads are falling back to local CSVs",
        )
        fix(
            "This is fine for local dev. For the Humana demo you want BigQuery:",
            "1. gcloud auth application-default login",
            "2. export GOOGLE_CLOUD_PROJECT=<project id>",
            "3. load the data (needs the bq CLI -- easiest in Cloud Shell):",
            "   cd caregap_compass/data/runtime/reference",
            "   chmod +x import-bq.sh && ./import-bq.sh ../extracted/structured",
            "4. re-run this doctor",
        )
        return False
    return True


def check_tables(on_bigquery: bool) -> None:
    section("tables")
    from .. import bq

    for table, expected in sorted(EXPECTED_ROWS.items()):
        try:
            rows = len(bq.fetch_table(table))
        except Exception as exc:  # noqa: BLE001
            check(f"{table}", FAIL, f"{type(exc).__name__}: {str(exc)[:90]}")
            continue
        if rows == expected:
            check(f"{table:<24}", OK, f"{rows} rows")
        else:
            check(
                f"{table:<24}",
                FAIL,
                f"{rows} rows, expected {expected} -- partial load or drifted data",
            )
            if on_bigquery:
                fix(f"bq load --replace --autodetect --skip_leading_rows=1 "
                    f"{config.BQ_DATASET}.{table} <csv>")


def check_parity() -> None:
    """CSV and BigQuery must agree, or the demo says different numbers depending
    on which one happens to be up."""
    section("csv / bigquery parity")
    from .. import bq

    if bq.backend() != "bigquery":
        check("parity", WARN, "skipped -- BigQuery is not the active backend")
        return

    import csv as _csv

    mismatches = []
    for table, expected in sorted(EXPECTED_ROWS.items()):
        path = config.STRUCTURED_DIR / f"{table}.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            csv_rows = len(list(_csv.DictReader(handle)))
        bq_rows = len(bq.fetch_table(table))
        if csv_rows != bq_rows:
            mismatches.append(f"{table}: csv={csv_rows} bq={bq_rows}")
    if mismatches:
        check("row counts agree", FAIL, "; ".join(mismatches))
        fix("Re-run import-bq.sh -- BigQuery is stale relative to the CSVs.")
    else:
        check("row counts agree", OK, f"{len(EXPECTED_ROWS)} tables match")

    # The number that gets said out loud. If this drifts, the deck is wrong.
    try:
        from .. import scoring

        ranking = scoring.rank_gaps(
            config.HERO_MEMBER_ID,
            bq.get_open_gaps(config.HERO_MEMBER_ID),
            bq.get_dispositions_for_member(config.HERO_MEMBER_ID),
            bq.get_interventions(),
        )
        selected = ranking.get("selected") or {}
        good = selected.get("measure_id") == "CBP" and abs(selected.get("score", 0) - 0.655) < 0.01
        check(
            "hero ranking on this backend",
            OK if good else FAIL,
            f"{selected.get('measure_id')} @ {selected.get('score')} (want CBP @ ~0.655)",
        )
    except Exception as exc:  # noqa: BLE001
        check("hero ranking on this backend", FAIL, f"{type(exc).__name__}: {exc}")


def check_clock() -> None:
    section("demo clock")
    from .. import bq

    today = config.today().isoformat()
    check("DEMO_TODAY", OK, today)
    try:
        slots = bq.get_slots()
    except Exception:  # noqa: BLE001
        check("bookable slots", FAIL, "could not read appointment_slots")
        return
    usable = [s for s in slots if s.get("available") and str(s.get("slot_date")) >= today]
    if usable:
        check("bookable slots", OK, f"{len(usable)} of {len(slots)} are open and in the future")
    else:
        check(
            "bookable slots",
            FAIL,
            f"0 of {len(slots)} bookable -- every slot is in the past, nothing can be booked",
        )
        fix(
            "The dataset's slots run 2026-06-11..2026-07-10.",
            "Set DEMO_TODAY=2026-06-10 (the stars report's own date).",
        )


def check_repo_hygiene() -> None:
    """You are pushing this to a Humana-connected repo. No secrets."""
    section("repo hygiene")
    env_path = config.REPO_ROOT / ".env"
    if env_path.exists():
        check(".env exists", OK, str(env_path))
        try:
            body = env_path.read_text(encoding="utf-8", errors="replace")
            has_key = any(
                line.strip().startswith(("GOOGLE_API_KEY=", "GEMINI_API_KEY="))
                and len(line.split("=", 1)[1].strip()) > 4
                for line in body.splitlines()
            )
            if has_key:
                check(
                    "no API key in .env",
                    WARN,
                    "an API key is present. It is gitignored, but for the Humana "
                    "project prefer Vertex + ADC so there is no secret at all.",
                )
            else:
                check("no API key in .env", OK, "no secret material")
        except OSError:
            pass
    else:
        check(".env exists", WARN, "not found -- copy .env.example to .env")

    gitignore = config.REPO_ROOT / ".gitignore"
    if gitignore.exists():
        ignored = gitignore.read_text(encoding="utf-8", errors="replace")
        check(
            ".env is gitignored",
            OK if "\n.env" in "\n" + ignored else FAIL,
            "secrets stay out of the repo",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CareGap Compass environment doctor")
    parser.add_argument(
        "--model",
        action="store_true",
        help="spend one real model call to prove Gemini is reachable",
    )
    args = parser.parse_args(argv)

    print()
    print("=" * 72)
    print("  CareGap Compass -- environment doctor")
    print("=" * 72)
    print(f"       python  {sys.version.split()[0]}")
    print(f"       repo    {config.REPO_ROOT}")

    mode = check_auth()
    check_model(mode, args.model)
    on_bq = check_bigquery(mode)
    check_tables(on_bq)
    check_parity()
    check_clock()
    check_repo_hygiene()

    print()
    print("=" * 72)
    if _failures:
        print(f"  {len(_failures)} FAILED, {len(_warnings)} warning(s)")
        for name in _failures:
            print(f"    FAIL  {name}")
        for name in _warnings:
            print(f"    warn  {name}")
        return 1
    if _warnings:
        print(f"  no failures, {len(_warnings)} warning(s)")
        for name in _warnings:
            print(f"    warn  {name}")
        return 0
    print("  everything green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
