"""The single data door for CareGap Compass.

Backend is BigQuery (dataset `humana_hackathon`, loaded by
data/runtime/reference/import-bq.sh) with a local-CSV fallback behind identical
accessors. Every read goes through the cache-aside layer in `cache.py`.

Backend parity matters more than it looks: `bq load --autodetect` yields real
types (bool/int/float) while csv.DictReader yields strings ('True', '3'). Callers
must never have to care which is live, so every row is coerced through TABLE_TYPES
on the way out of both backends.
"""

from __future__ import annotations

import csv
import logging
import threading
from typing import Any, Callable

from . import config
from .cache import LFUCache

logger = logging.getLogger(__name__)

_cache = LFUCache(capacity=config.BQ_CACHE_CAPACITY, ttl=config.BQ_CACHE_TTL)
_client = None
_client_lock = threading.Lock()
_backend_resolved: str | None = None


class DataUnavailable(RuntimeError):
    """Raised when a table cannot be read. Tools surface this as a clean error
    rather than letting the model narrate around missing data."""


# --------------------------------------------------------------------------
# Type coercion
# --------------------------------------------------------------------------

INT = "int"
FLOAT = "float"
BOOL = "bool"

TABLE_TYPES: dict[str, dict[str, str]] = {
    "members": {
        "age": INT,
        "lat": FLOAT,
        "lon": FLOAT,
        "chronic_diabetes": BOOL,
        "chronic_hypertension": BOOL,
        "chronic_cardiovascular": BOOL,
    },
    "care_gaps": {"outreach_attempts": INT, "care_gap_year": INT},
    "stars_performance": {
        "measurement_year": INT,
        "is_scored_star_measure": BOOL,
        "plan_rate_pct": FLOAT,
        "benchmark_2star_pct": FLOAT,
        "benchmark_3star_pct": FLOAT,
        "benchmark_4star_pct": FLOAT,
        "benchmark_5star_pct": FLOAT,
        "current_star_rating": INT,
        "members_eligible": INT,
        "members_compliant": INT,
        "gap_count": INT,
        "at_risk": BOOL,
        "prior_year_rate_pct": FLOAT,
    },
    "providers": {"lat": FLOAT, "lon": FLOAT},
    "appointment_slots": {
        "duration_min": INT,
        "telehealth": BOOL,
        "lat": FLOAT,
        "lon": FLOAT,
        "available": BOOL,
    },
    "campaign_dispositions": {
        "attempt_number": INT,
        "gap_credited_in_system": BOOL,
        "actual_completion_likely": BOOL,
    },
    "historical_interventions": {
        "intervention_year": INT,
        "members_targeted": INT,
        "members_closed": INT,
        "closure_rate_pct": FLOAT,
        "cost_per_closure_usd": FLOAT,
        "total_cost_est_usd": FLOAT,
    },
    "coverage_rules": {
        "covered": BOOL,
        "prior_auth_required": BOOL,
        "cost_share_pct": FLOAT,
        "copay": FLOAT,
    },
    "segment_performance": {
        "members_eligible": INT,
        "members_compliant": INT,
        "rate_pct": FLOAT,
    },
    "roi_authorizations": {"auth_on_file": BOOL, "auth_expired": BOOL},
    "claims": {
        "billed_amount": FLOAT,
        "paid_amount": FLOAT,
        "referral_on_file": BOOL,
        "prior_auth_required": BOOL,
        "prior_auth_obtained": BOOL,
        "denial_risk_flag": BOOL,
        "modifier_mismatch": BOOL,
        "denial_fixable": BOOL,
        "reprocessing_days_est": FLOAT,
    },
    "compliance_flags": {"metric_value": FLOAT, "resolved": BOOL},
}

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


def _coerce(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    if kind == BOOL:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        return None
    if kind == INT:
        if isinstance(value, bool):
            return int(value)
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    if kind == FLOAT:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return value


def _coerce_rows(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    types = TABLE_TYPES.get(table, {})
    out = []
    for row in rows:
        clean = dict(row)
        for column, kind in types.items():
            if column in clean:
                clean[column] = _coerce(clean[column], kind)
        for key, value in clean.items():
            if isinstance(value, str):
                clean[key] = value.strip()
        out.append(clean)
    return out


# --------------------------------------------------------------------------
# Backend resolution
# --------------------------------------------------------------------------


def _bigquery_client():
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        from google.cloud import bigquery  # imported lazily; optional dependency

        _client = bigquery.Client(
            project=config.GOOGLE_CLOUD_PROJECT or None,
            location=config.BQ_LOCATION,
        )
        return _client


def _bigquery_reachable() -> bool:
    try:
        client = _bigquery_client()
        client.query("SELECT 1").result(timeout=20)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "use CSV"
        logger.warning("BigQuery unavailable (%s: %s)", type(exc).__name__, exc)
        return False


def backend() -> str:
    """Resolve the active backend once per process: 'bigquery' or 'csv'."""
    global _backend_resolved
    if _backend_resolved is not None:
        return _backend_resolved

    requested = config.DATA_BACKEND
    if requested == "csv":
        _backend_resolved = "csv"
    elif requested == "bigquery":
        if not _bigquery_reachable():
            raise DataUnavailable(
                "DATA_BACKEND=bigquery but BigQuery is not reachable. Check "
                "GOOGLE_CLOUD_PROJECT and application default credentials, or set "
                "DATA_BACKEND=csv / auto."
            )
        _backend_resolved = "bigquery"
    else:
        _backend_resolved = "bigquery" if _bigquery_reachable() else "csv"
        if _backend_resolved == "csv":
            logger.warning(
                "Falling back to local CSVs at %s. Reads are identical; only the "
                "source differs.",
                config.STRUCTURED_DIR,
            )

    logger.info("CareGap Compass data backend: %s", _backend_resolved)
    return _backend_resolved


def backend_info() -> dict[str, Any]:
    return {
        "backend": backend(),
        "dataset": config.BQ_DATASET,
        "project": config.GOOGLE_CLOUD_PROJECT or "(unset)",
        "csv_dir": str(config.STRUCTURED_DIR),
        "cache": _cache.stats(),
    }


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def _read_csv(table: str) -> list[dict[str, Any]]:
    path = config.STRUCTURED_DIR / f"{table}.csv"
    if not path.exists():
        raise DataUnavailable(f"No CSV for table '{table}' at {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _run_query(sql: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
    from google.cloud import bigquery

    def _param(name: str, value: Any):
        if isinstance(value, bool):
            kind = "BOOL"
        elif isinstance(value, int):
            kind = "INT64"
        elif isinstance(value, float):
            kind = "FLOAT64"
        else:
            kind = "STRING"
            value = str(value)
        return bigquery.ScalarQueryParameter(name, kind, value)

    job_config = bigquery.QueryJobConfig(
        query_parameters=[_param(k, v) for k, v in (params or {}).items()]
    )
    try:
        rows = _bigquery_client().query(sql, job_config=job_config).result()
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        raise DataUnavailable(f"BigQuery read failed: {exc}") from exc


def _cached(key: tuple, loader: Callable[[], list[dict[str, Any]]]):
    return _cache.get_or_load(key, loader)


def fetch_table(table: str) -> list[dict[str, Any]]:
    """Whole-table read. Every table here is small (<=880 rows), so this is cheap
    and lets the CSV and BigQuery paths stay byte-for-byte equivalent."""

    def load() -> list[dict[str, Any]]:
        if backend() == "bigquery":
            rows = _run_query(
                f"SELECT * FROM `{config.BQ_DATASET}.{table}`", None
            )
        else:
            rows = _read_csv(table)
        return _coerce_rows(table, rows)

    return _cached(("table", table), load)


def fetch_where(table: str, column: str, value: Any) -> list[dict[str, Any]]:
    """Single-column filter, parameterized on BigQuery."""

    def load() -> list[dict[str, Any]]:
        if backend() == "bigquery":
            rows = _run_query(
                f"SELECT * FROM `{config.BQ_DATASET}.{table}` WHERE {column} = @value",
                {"value": value},
            )
            return _coerce_rows(table, rows)
        return [r for r in fetch_table(table) if r.get(column) == value]

    return _cached(("where", table, column, value), load)


# --------------------------------------------------------------------------
# Typed accessors
# --------------------------------------------------------------------------


def get_member(member_id: str) -> dict[str, Any] | None:
    rows = fetch_where("members", "member_id", member_id)
    return rows[0] if rows else None


def list_member_ids() -> list[str]:
    return [r["member_id"] for r in fetch_table("members")]


def get_open_gaps(member_id: str) -> list[dict[str, Any]]:
    return [
        r
        for r in fetch_where("care_gaps", "member_id", member_id)
        if str(r.get("gap_status", "")).lower() == "open"
    ]


def get_gap(gap_id: str) -> dict[str, Any] | None:
    rows = fetch_where("care_gaps", "gap_id", gap_id)
    return rows[0] if rows else None


def get_all_gaps() -> list[dict[str, Any]]:
    return fetch_table("care_gaps")


def get_stars() -> list[dict[str, Any]]:
    return fetch_table("stars_performance")


def get_star_measure(measure_id: str) -> dict[str, Any] | None:
    for row in get_stars():
        if row.get("measure_id") == measure_id:
            return row
    return None


def get_dispositions_for_member(member_id: str) -> list[dict[str, Any]]:
    return fetch_where("campaign_dispositions", "member_id", member_id)


def get_interventions() -> list[dict[str, Any]]:
    return fetch_table("historical_interventions")


def get_providers() -> list[dict[str, Any]]:
    return fetch_table("providers")


def get_slots() -> list[dict[str, Any]]:
    return fetch_table("appointment_slots")


def get_coverage_rules() -> list[dict[str, Any]]:
    return fetch_table("coverage_rules")


def get_segment_performance() -> list[dict[str, Any]]:
    return fetch_table("segment_performance")


def cache_stats() -> dict[str, Any]:
    return _cache.stats()


def clear_cache() -> None:
    _cache.clear()
