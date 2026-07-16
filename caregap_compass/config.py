"""Central configuration for CareGap Compass.

Every date computation in this project must route through `today()`. The synthetic
dataset was generated around 2026-06-10 (the date on stars_performance_report.md):
appointment slots run 2026-06-11..2026-07-10 and care-gap due dates fan out from
mid-2026. Using the real wall clock puts every slot in the past and drives
days-to-close negative, which silently breaks both scoring and booking.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

DATA_ROOT = PACKAGE_ROOT / "data" / "runtime"
STRUCTURED_DIR = DATA_ROOT / "extracted" / "structured"
UNSTRUCTURED_DIR = DATA_ROOT / "extracted" / "unstructured"
TRANSCRIPTS_DIR = UNSTRUCTURED_DIR / "call_transcripts"

AUDIT_FILE = DATA_ROOT / "audit.jsonl"
COMPLIANCE_FLAG_FILE = DATA_ROOT / "compliance_flags.jsonl"
FEEDBACK_DB = Path(os.getenv("FEEDBACK_DB", DATA_ROOT / "feedback.db"))

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
BQ_DATASET = os.getenv("BQ_DATASET", "humana_hackathon")
BQ_LOCATION = os.getenv("BQ_LOCATION", "US")

# "bigquery" | "csv" | "auto". "auto" prefers BigQuery and falls back to local CSVs
# when the client, credentials, or project are unavailable.
DATA_BACKEND = os.getenv("DATA_BACKEND", "auto").lower()

MODEL = os.getenv("CARE_GAP_MODEL", "gemini-flash-latest")

BQ_CACHE_CAPACITY = int(os.getenv("BQ_CACHE_CAPACITY", "128"))
BQ_CACHE_TTL = float(os.getenv("BQ_CACHE_TTL", "300"))

HERO_MEMBER_ID = os.getenv("HERO_MEMBER_ID", "MBR00030")

_DEMO_TODAY = os.getenv("DEMO_TODAY", "2026-06-10")

# Urgency horizon in days: roughly the actionable remainder of the measurement
# year. A gap due in 40 days scores 1 - 40/270 = 0.85 urgency.
URGENCY_HORIZON_DAYS = int(os.getenv("URGENCY_HORIZON_DAYS", "270"))

# Applied when a member has no disposition history on a channel: we assume neither
# responsiveness nor refusal rather than scoring the channel to zero.
NEUTRAL_CHANNEL_PRIOR = float(os.getenv("NEUTRAL_CHANNEL_PRIOR", "0.5"))


def today() -> _dt.date:
    """The demo clock. Never call `date.today()` anywhere else in this project."""
    return _dt.date.fromisoformat(_DEMO_TODAY)


def now_iso() -> str:
    """Timestamp for audit/compliance records, pinned to the demo clock."""
    return _dt.datetime.combine(today(), _dt.time(12, 0)).isoformat()
