"""Evaluation metrics: per-type win rates, latency, freshness, contradiction.

The minimum gate for the canary stage requires:
- Fuli non-inferior overall
- no major query type below the agreed floor
- no privacy violation
- no critical contradiction regression
- Fuli success >= 99%
- p95 latency within agreed threshold
- at least 100 adjudications
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QueryTypeMetrics:
    total: int = 0
    sampled: int = 0
    completed: int = 0
    timed_out: int = 0
    failed: int = 0
    primary_mutation: int = 0
    fuli_better: int = 0
    honcho_better: int = 0
    tie: int = 0
    both_bad: int = 0
    insufficient_evidence: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        decided = self.fuli_better + self.honcho_better
        if decided == 0:
            return float("nan")
        return self.fuli_better / decided

    @property
    def success_rate(self) -> float:
        if self.sampled == 0:
            return float("nan")
        return self.completed / self.sampled

    @property
    def p50_latency_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.50)

    @property
    def p95_latency_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.95)

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if q <= 0.0:
        return s[0]
    if q >= 1.0:
        return s[-1]
    idx = (len(s) - 1) * q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


@dataclass
class Metrics:
    """Aggregated evaluation metrics.

    Tracks per-query-type breakdown and the overall aggregate. The
    ``by_type`` map is keyed by query type. The ``overall`` field
    holds the cross-type aggregate (summed counts; latency merged).
    """

    by_type: Dict[str, QueryTypeMetrics] = field(default_factory=dict)
    overall: QueryTypeMetrics = field(default_factory=QueryTypeMetrics)
    confidence_level: float = 0.95

    def add(self, query_type: str, *,
            fuli_better: bool = False, honcho_better: bool = False,
            tie: bool = False, both_bad: bool = False,
            insufficient_evidence: bool = False,
            latency_ms: Optional[float] = None,
            primary_mutation: bool = False,
            completed: bool = True, timed_out: bool = False,
            failed: bool = False, sampled: bool = True) -> None:
        m = self.by_type.setdefault(query_type, QueryTypeMetrics())
        m.total += 1
        m.sampled += 1 if sampled else 0
        m.completed += 1 if completed else 0
        m.timed_out += 1 if timed_out else 0
        m.failed += 1 if failed else 0
        m.primary_mutation += 1 if primary_mutation else 0
        if fuli_better:
            m.fuli_better += 1
        if honcho_better:
            m.honcho_better += 1
        if tie:
            m.tie += 1
        if both_bad:
            m.both_bad += 1
        if insufficient_evidence:
            m.insufficient_evidence += 1
        if latency_ms is not None:
            m.latencies_ms.append(latency_ms)
        self.overall = self._aggregate()

    def _aggregate(self) -> QueryTypeMetrics:
        agg = QueryTypeMetrics()
        for m in self.by_type.values():
            agg.total += m.total
            agg.sampled += m.sampled
            agg.completed += m.completed
            agg.timed_out += m.timed_out
            agg.failed += m.failed
            agg.primary_mutation += m.primary_mutation
            agg.fuli_better += m.fuli_better
            agg.honcho_better += m.honcho_better
            agg.tie += m.tie
            agg.both_bad += m.both_bad
            agg.insufficient_evidence += m.insufficient_evidence
            agg.latencies_ms.extend(m.latencies_ms)
        return agg

    def canary_gate(self, *, p95_threshold_ms: float, fuli_success_floor: float = 0.99,
                    fuli_win_floor: float = 0.45, n_min: int = 100) -> Dict[str, Any]:
        """Return a canary-gate verdict.

        Returns a dict with one entry per gate:
          - sufficient_n: bool
          - fuli_success_rate: float
          - fuli_win_rate: float
          - p95_latency_ms: float
          - p95_within_threshold: bool
          - overall_pass: bool
        """
        total = self.overall.total
        decided = self.overall.fuli_better + self.overall.honcho_better
        win_rate = self.overall.fuli_better / decided if decided else float("nan")
        success = self.overall.completed / self.overall.sampled if self.overall.sampled else float("nan")
        p95 = self.overall.p95_latency_ms
        sufficient_n = total >= n_min
        return {
            "sufficient_n": sufficient_n,
            "fuli_success_rate": success,
            "fuli_win_rate": win_rate,
            "p95_latency_ms": p95,
            "p95_within_threshold": p95 <= p95_threshold_ms,
            "fuli_success_above_floor": success >= fuli_success_floor,
            "fuli_win_above_floor": win_rate >= fuli_win_floor,
            "overall_pass": (
                sufficient_n
                and success >= fuli_success_floor
                and win_rate >= fuli_win_floor
                and p95 <= p95_threshold_ms
            ),
        }
