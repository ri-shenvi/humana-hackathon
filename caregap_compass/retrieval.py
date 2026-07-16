"""Grounded retrieval over the unstructured corpus.

BM25 over the call transcripts and the STARs report, in-process. No vector
database, no embedding API, no network call -- which means no cold start and no
external dependency to fail during a recorded demo.

The interface is the part that matters: search(query, k) -> passages with a
source. Swapping BM25 for Vertex embeddings later is a change to this file and
nothing else.

Every passage carries the file it came from, because a claim the agent cannot
attribute is a claim it should not make.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from typing import Any

from . import config, privacy

_INDEX_LOCK = threading.Lock()
_index: "_Bm25Index | None" = None

_TOKEN = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    """a an and are as at be by for from has have i if in is it its of on or that the
    to was were will with you your my me we they this these those there here what
    when where which who how do does did can could would should not no yes""".split()
)

# Member IDs and claim IDs appear throughout the transcripts. They are useful as
# search terms but must never be echoed back, so they are masked at index time --
# what is never indexed cannot leak into an answer.
_MEMBER_ID = re.compile(r"\bMBR\d{5}\b")
_CLAIM_ID = re.compile(r"\b(CLM|GAP|DISP|AUTH)\d{6}\b")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


def _scrub(text: str) -> str:
    text = _MEMBER_ID.sub(lambda m: privacy.mask_member_id(m.group()), text)
    return _CLAIM_ID.sub(lambda m: m.group()[:3] + "***", text)


class _Bm25Index:
    """Standard BM25. k1 controls term-frequency saturation, b length
    normalization; the defaults are the usual ones and are not tuned."""

    K1 = 1.5
    B = 0.75

    def __init__(self, passages: list[dict[str, Any]]) -> None:
        self.passages = passages
        self.tokens = [_tokenize(p["text"]) for p in passages]
        self.lengths = [len(t) for t in self.tokens]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.freqs = [Counter(t) for t in self.tokens]

        doc_freq: Counter[str] = Counter()
        for tokens in self.tokens:
            doc_freq.update(set(tokens))
        total = len(passages)
        self.idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in doc_freq.items()
        }

    def search(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        terms = _tokenize(query)
        if not terms or not self.passages:
            return []
        scored = []
        for index, freq in enumerate(self.freqs):
            score = 0.0
            length = self.lengths[index] or 1
            for term in terms:
                if term not in freq:
                    continue
                tf = freq[term]
                denom = tf + self.K1 * (
                    1 - self.B + self.B * length / (self.avg_length or 1)
                )
                score += self.idf.get(term, 0.0) * (tf * (self.K1 + 1)) / denom
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [
            {**self.passages[index], "score": round(score, 3)}
            for score, index in scored[:k]
        ]


def _chunk_transcript(path) -> list[dict[str, Any]]:
    """One passage per exchange, not per file. A whole 12-minute call is too
    coarse to be evidence for anything."""
    raw = _scrub(path.read_text(encoding="utf-8", errors="replace"))
    scenario = ""
    for line in raw.splitlines():
        if line.lower().startswith("scenario:"):
            scenario = line.split(":", 1)[1].strip()
            break

    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
    passages = []
    for position, block in enumerate(blocks):
        if len(_tokenize(block)) < 4:
            continue
        passages.append(
            {
                "text": block,
                "source": path.name,
                "source_type": "call_transcript",
                "scenario": scenario,
                "position": position,
            }
        )
    return passages


def _chunk_markdown(path) -> list[dict[str, Any]]:
    raw = _scrub(path.read_text(encoding="utf-8", errors="replace"))
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
    return [
        {
            "text": block,
            "source": path.name,
            "source_type": "stars_report",
            "scenario": "",
            "position": position,
        }
        for position, block in enumerate(blocks)
        if len(_tokenize(block)) >= 4
    ]


def _build() -> _Bm25Index:
    passages: list[dict[str, Any]] = []
    if config.TRANSCRIPTS_DIR.exists():
        for path in sorted(config.TRANSCRIPTS_DIR.glob("*.txt")):
            passages.extend(_chunk_transcript(path))
    report = config.UNSTRUCTURED_DIR / "stars_performance_report.md"
    if report.exists():
        passages.extend(_chunk_markdown(report))
    return _Bm25Index(passages)


def index() -> _Bm25Index:
    global _index
    with _INDEX_LOCK:
        if _index is None:
            _index = _build()
        return _index


def search(query: str, k: int = 3) -> list[dict[str, Any]]:
    """Top-k passages for a query, each with the file it came from."""
    return index().search(query, k)


def search_member_language(measure_name: str, k: int = 2) -> list[dict[str, Any]]:
    """What members actually say about this measure, in their words.

    Restricted to transcripts: the STARs report is written for executives and
    lifting its phrasing into a member conversation is exactly the wrong register.
    """
    hits = index().search(measure_name, k * 4)
    return [h for h in hits if h["source_type"] == "call_transcript"][:k]


def stats() -> dict[str, Any]:
    built = index()
    sources = Counter(p["source_type"] for p in built.passages)
    return {
        "passages": len(built.passages),
        "by_type": dict(sources),
        "vocabulary": len(built.idf),
        "avg_passage_tokens": round(built.avg_length, 1),
        "method": "BM25 (k1=1.5, b=0.75), in-process",
    }
