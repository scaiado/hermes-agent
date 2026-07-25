"""Sampling, query-type classification, and metrics aggregation.

The sampling logic is deterministic per run_id and is independent of the
memory providers.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QUERY_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "profile": ["who is", "about", "profile", "user"],
    "preference": ["prefer", "like", "want", "usually", "always", "never"],
    "project": ["project", "repo", "codebase", "build", "deploy"],
    "episodic": ["yesterday", "last", "recently", "this morning", "today", "earlier"],
    "exact": ["what is my", "where is", "when did", "find the"],
    "semantic": ["relevant", "similar", "related to", "about"],
    "contradiction": ["but", "however", "previously", "earlier you said"],
}


@dataclass
class SamplingDecision:
    sampled: bool
    query_type: str
    run_id: str
    reason: str = ""


def classify_query_type(query: str) -> str:
    """Heuristic query-type classification for stratified evaluation."""
    q = (query or "").lower()
    scores: Dict[str, int] = {}
    for qtype, keywords in QUERY_TYPE_KEYWORDS.items():
        scores[qtype] = sum(1 for kw in keywords if kw in q)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] == 0:
        return "unclassified"
    return best


def should_sample(
    run_id: str,
    query_type: str,
    sample_rate: float,
    seed: int = 0,
    force_types: Optional[List[str]] = None,
) -> SamplingDecision:
    """Deterministic sampling decision per run_id.

    Sampling is stable: the same run_id always produces the same decision. This
    makes unit tests reproducible and prevents double-counting on retries.
    """
    if sample_rate <= 0.0:
        return SamplingDecision(sampled=False, query_type=query_type, run_id=run_id, reason="sample_rate_zero")

    if force_types and query_type in force_types:
        return SamplingDecision(sampled=True, query_type=query_type, run_id=run_id, reason="forced_type")

    digest = hashlib.sha256(f"{seed}:{run_id}:{query_type}".encode("utf-8")).hexdigest()
    value = int(digest[:16], 16) / (2**64 - 1)
    sampled = value < sample_rate
    return SamplingDecision(
        sampled=sampled,
        query_type=query_type,
        run_id=run_id,
        reason="sampled" if sampled else "not_sampled",
    )


@dataclass
class QueryTypeMetrics:
    total: int = 0
    sampled: int = 0
    completed: int = 0
    timed_out: int = 0
    failed: int = 0
    primary_mutation: int = 0


@dataclass
class Metrics:
    by_type: Dict[str, QueryTypeMetrics] = field(default_factory=dict)
    overall: QueryTypeMetrics = field(default_factory=QueryTypeMetrics)

    def aggregate(self, rows: List[Dict[str, Any]]) -> None:
        """Aggregate a list of comparison row dicts into metrics."""
        for r in rows:
            qtype = r.get("query_type", "unclassified")
            self._add(qtype, r)
            self._add("overall", r)

    def _add(self, key: str, r: Dict[str, Any]) -> None:
        m = self.by_type.setdefault(key, QueryTypeMetrics())
        m.total += 1
        if r.get("completed_at") is not None:
            m.completed += 1
        if r.get("timed_out"):
            m.timed_out += 1
        if r.get("error"):
            m.failed += 1
        if r.get("primary_mutation"):
            m.primary_mutation += 1
