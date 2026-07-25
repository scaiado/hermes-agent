"""Evaluation design: representative corpus, blind adjudication, stratification.

This module supports the representative quality evaluation gate required before
CANARY or PRIMARY mode.
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fuli_product.sampling import classify_query_type

logger = logging.getLogger(__name__)

REASON_CODES = {
    "A_more_relevant": "A results were more relevant to the query",
    "B_more_relevant": "B results were more relevant to the query",
    "A_more_complete": "A results were more complete",
    "B_more_complete": "B results were more complete",
    "A_more_recent": "A results were more recent",
    "B_more_recent": "B results were more recent",
    "A_more_accurate": "A results were more accurate",
    "B_more_accurate": "B results were more accurate",
    "tie": "Results were equivalent",
    "inconclusive": "Could not determine a winner",
}


@dataclass
class EvaluationQuery:
    query_type: str
    query: str
    expected: Optional[str] = None
    priority: int = 1


@dataclass
class EvaluationCorpus:
    queries: List[EvaluationQuery] = field(default_factory=list)

    def add(self, query_type: str, query: str, expected: Optional[str] = None, priority: int = 1) -> None:
        self.queries.append(EvaluationQuery(query_type, query, expected, priority))

    def by_type(self) -> Dict[str, List[EvaluationQuery]]:
        groups: Dict[str, List[EvaluationQuery]] = {}
        for q in self.queries:
            groups.setdefault(q.query_type, []).append(q)
        return groups


@dataclass
class AdjudicationResult:
    comparison_id: str
    winner: str  # "A", "B", or "tie"
    reason_code: str
    query_type: str
    adjudicator: str
    timestamp: str


class Adjudication:
    """Blind adjudication of comparison results."""

    def __init__(self, results: List[AdjudicationResult]) -> None:
        self.results = results

    def summary(self) -> Dict[str, Any]:
        total = len(self.results)
        wins_a = sum(1 for r in self.results if r.winner == "A")
        wins_b = sum(1 for r in self.results if r.winner == "B")
        ties = sum(1 for r in self.results if r.winner == "tie")
        by_type: Dict[str, Dict[str, int]] = {}
        for r in self.results:
            by_type.setdefault(r.query_type, {"A": 0, "B": 0, "tie": 0})
            by_type[r.query_type][r.winner] += 1
        return {
            "total": total,
            "wins_a": wins_a,
            "wins_b": wins_b,
            "ties": ties,
            "by_type": by_type,
        }

    def is_non_inferior(self, min_margin: float = 0.0) -> bool:
        """Return True if A is not statistically worse than B by at least min_margin."""
        s = self.summary()
        total = s["total"] - s["ties"]
        if total == 0:
            return False
        return (s["wins_a"] / total) >= (s["wins_b"] / total) - min_margin


class StratifiedSampler:
    """Draw a stratified sample from the comparison store for evaluation."""

    def __init__(self, min_per_type: int = 10, max_per_type: int = 50) -> None:
        self.min_per_type = min_per_type
        self.max_per_type = max_per_type

    def sample(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return up to max_per_type rows per query type, sorted by recency."""
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_type.setdefault(r["query_type"], []).append(r)
        sampled = []
        for qtype, group in by_type.items():
            group = sorted(group, key=lambda x: x.get("started_at", 0), reverse=True)
            sampled.extend(group[: self.max_per_type])
        return sampled

    def coverage(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Return count per query type."""
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["query_type"]] = counts.get(r["query_type"], 0) + 1
        return counts

    def is_representative(self, rows: List[Dict[str, Any]], required_types: Optional[List[str]] = None) -> bool:
        """Check if the corpus has at least min_per_type samples for each required type."""
        required_types = required_types or [
            "profile", "preference", "project", "episodic", "exact", "semantic", "recent", "contradiction"
        ]
        coverage = self.coverage(rows)
        return all(coverage.get(t, 0) >= self.min_per_type for t in required_types)


def default_evaluation_corpus() -> EvaluationCorpus:
    """Return a minimal representative evaluation corpus."""
    corpus = EvaluationCorpus()
    corpus.add("profile", "What is my name and role?")
    corpus.add("profile", "Summarize who I am based on my memories.")
    corpus.add("preference", "What do I prefer for my morning routine?")
    corpus.add("preference", "What are my dietary restrictions?")
    corpus.add("project", "What project was I working on last week?")
    corpus.add("project", "Summarize the current codebase architecture.")
    corpus.add("episodic", "What did I do yesterday?")
    corpus.add("episodic", "What happened in our last meeting?")
    corpus.add("exact", "What is my OpenAI API key?")
    corpus.add("exact", "What is my home server IP?")
    corpus.add("semantic", "Find memories related to machine learning.")
    corpus.add("semantic", "What do I know about vector databases?")
    corpus.add("recent", "What were my last tasks?")
    corpus.add("recent", "What did I ask you to do recently?")
    corpus.add("contradiction", "I said I never eat cheese, but earlier you said I like cheese. Which is true?")
    return corpus
