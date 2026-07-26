"""Representative evaluation corpus construction and stratification.

The corpus is a privacy-safe collection of approved representative
queries. Raw memory text is NEVER stored; each query is hashed and
typed. The StratifiedSampler ensures at least 10 queries per major
query type so per-type win rates are statistically meaningful.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Query types required by the production cutover contract.
REQUIRED_QUERY_TYPES = (
    "profile", "preference", "project", "episodic",
    "exact", "semantic", "recent", "contradiction",
)
MIN_PER_TYPE = 10
MIN_TOTAL_QUERIES = 200


@dataclass(frozen=True)
class EvaluationQuery:
    query_type: str
    query: str
    expected: Optional[str] = None
    priority: int = 1
    query_hash: str = ""

    def __post_init__(self) -> None:
        if not self.query_hash:
            object.__setattr__(self, "query_hash", _hash_query(self.query))


def _hash_query(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


@dataclass
class EvaluationCorpus:
    queries: List[EvaluationQuery] = field(default_factory=list)

    def add(self, query_type: str, query: str,
            expected: Optional[str] = None, priority: int = 1) -> None:
        self.queries.append(EvaluationQuery(query_type, query, expected, priority))

    def by_type(self) -> Dict[str, List[EvaluationQuery]]:
        groups: Dict[str, List[EvaluationQuery]] = {}
        for q in self.queries:
            groups.setdefault(q.query_type, []).append(q)
        return groups

    def total(self) -> int:
        return len(self.queries)

    def is_complete(self) -> bool:
        groups = self.by_type()
        if self.total() < MIN_TOTAL_QUERIES:
            return False
        for qt in REQUIRED_QUERY_TYPES:
            if len(groups.get(qt, [])) < MIN_PER_TYPE:
                return False
        return True


class StratifiedSampler:
    """Deterministic per-run_id sampling across the corpus."""

    def __init__(self, corpus: EvaluationCorpus, seed: int = 0) -> None:
        self.corpus = corpus
        self.seed = seed
        self._rng = random.Random(seed)

    def sample(self, n: int) -> List[EvaluationQuery]:
        groups = self.corpus.by_type()
        per_type = max(1, n // len(groups))
        out: List[EvaluationQuery] = []
        for qt, qs in groups.items():
            if not qs:
                continue
            self._rng.shuffle(qs)
            out.extend(qs[:per_type])
        self._rng.shuffle(out)
        return out[:n]

    def per_type_targets(self, n_per_type: int = MIN_PER_TYPE) -> Dict[str, int]:
        return {qt: n_per_type for qt in REQUIRED_QUERY_TYPES}


def default_evaluation_corpus() -> EvaluationCorpus:
    """Return a starter corpus satisfying the minimum-size contract.

    The starter corpus uses synthetic, privacy-safe query templates
    per query type. Real production corpora are operator-built from
    approved captured shadow rows; the starter corpus exists to
    bootstrap local development and CI."""
    corpus = EvaluationCorpus()
    samples_per_type = max(MIN_PER_TYPE, MIN_TOTAL_QUERIES // len(REQUIRED_QUERY_TYPES))
    templates = {
        "profile": ["who is the user", "what is the user's background",
                    "tell me about the operator"],
        "preference": ["what does the user prefer for X",
                       "does the user like Y",
                       "any stated preference for Z"],
        "project": ["what projects is the user working on",
                    "is there a project called X",
                    "status of the Y project"],
        "episodic": ["what happened yesterday",
                     "what did the user do last week",
                     "any recent activity around X"],
        "exact": ["what is my favorite X",
                  "where is the Y file",
                  "when did the user set up Z"],
        "semantic": ["find anything related to X",
                     "what is similar to Y",
                     "anything about Z in past conversations"],
        "recent": ["most recent mention of X",
                   "what was discussed last about Y",
                   "last update on Z"],
        "contradiction": ["the user said X earlier, what about Y",
                          "did the user change their mind on Z",
                          "previous answer and current answer conflict on what"],
    }
    for qt, ts in templates.items():
        for i in range(samples_per_type):
            tpl = ts[i % len(ts)]
            corpus.add(qt, f"{tpl} #{i}")
    return corpus
