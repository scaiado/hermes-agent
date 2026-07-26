"""Non-inferiority calculator for the Fuli evaluation pipeline.

Given a list of adjudication mappings (winner/loser per case),
compute the Fuli-vs-Honcho win/loss summary, per-query-type
breakdown, contradiction-specific and temporal-specific
subtotals, latency percentiles, error rate, and bootstrap 95%
confidence intervals for the net preference difference.

The non-inferiority margin is defined BEFORE evaluating
results: Fuli may be no more than 10 percentage points worse
than Honcho overall. The canary quality gate requires:

- lower bound of Fuli-minus-Honcho preference difference > -10%;
- no major query type point estimate worse than -20%;
- contradiction cases not worse than -10%;
- no critical privacy or stale-memory failure;
- Fuli retrieval success >= 99%;
- Fuli p95 latency <= 500 ms;
- no hard operational gate failure.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


NON_INFERIORITY_MARGIN_PP = 0.10  # Fuli may be no more than 10pp worse
PER_TYPE_FLOOR_PP = 0.20
CONTRADICTION_FLOOR_PP = 0.10
FULI_SUCCESS_FLOOR = 0.99
FULI_P95_FLOOR_MS = 500.0


@dataclass(frozen=True)
class AdjudicationResult:
    case_id: str
    query_type: str
    contradiction: bool
    winner: str  # "honcho" | "fuli" | "tie" | "both_bad"
    latency_ms: float = 0.0
    fuli_success: bool = True


def _usable(results: Iterable[AdjudicationResult]) -> List[AdjudicationResult]:
    """Exclude ties and both_bad from the head-to-head comparison."""
    return [
        r for r in results
        if r.winner in ("honcho", "fuli")
    ]


def summary(results: List[AdjudicationResult]) -> Dict[str, Any]:
    """Return the full non-inferiority report.

    The structure is JSON-serializable. Bootstrap 95% confidence
    intervals are computed for the net preference difference
    (Fuli wins - Honcho wins) / usable N.
    """
    fuli_wins = sum(1 for r in results if r.winner == "fuli")
    honcho_wins = sum(1 for r in results if r.winner == "honcho")
    ties = sum(1 for r in results if r.winner == "tie")
    both_bad = sum(1 for r in results if r.winner == "both_bad")
    insufficient = sum(1 for r in results if r.winner == "insufficient_evidence")
    usable = _usable(results)
    usable_n = len(usable)
    fuli_win_share = (fuli_wins / usable_n) if usable_n else float("nan")
    net_preference = (
        (fuli_wins - honcho_wins) / usable_n if usable_n else float("nan")
    )

    by_type: Dict[str, Dict[str, int]] = {}
    for r in results:
        by_type.setdefault(r.query_type, {"fuli": 0, "honcho": 0, "tie": 0, "both_bad": 0})
        if r.winner in by_type[r.query_type]:
            by_type[r.query_type][r.winner] += 1

    by_type_usable: Dict[str, Dict[str, float]] = {}
    for qt, counts in by_type.items():
        u = counts["fuli"] + counts["honcho"]
        if u == 0:
            by_type_usable[qt] = {
                "fuli_win_share": float("nan"),
                "net_preference": float("nan"),
                "usable_n": 0,
            }
            continue
        by_type_usable[qt] = {
            "fuli_win_share": counts["fuli"] / u,
            "net_preference": (counts["fuli"] - counts["honcho"]) / u,
            "usable_n": u,
        }

    contradiction_results = [r for r in results if r.contradiction]
    contradiction_usable = _usable(contradiction_results)
    if contradiction_usable:
        fuli_c = sum(1 for r in contradiction_usable if r.winner == "fuli")
        honcho_c = sum(1 for r in contradiction_usable if r.winner == "honcho")
        contradiction_net = (fuli_c - honcho_c) / len(contradiction_usable)
    else:
        contradiction_net = float("nan")

    temporal_results = [r for r in results if r.query_type in ("recent", "episodic")]
    temporal_usable = _usable(temporal_results)
    if temporal_usable:
        fuli_t = sum(1 for r in temporal_usable if r.winner == "fuli")
        honcho_t = sum(1 for r in temporal_usable if r.winner == "honcho")
        temporal_net = (fuli_t - honcho_t) / len(temporal_usable)
    else:
        temporal_net = float("nan")

    latencies = sorted(r.latency_ms for r in results if r.latency_ms > 0)
    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
    pmax = latencies[-1] if latencies else 0.0
    fuli_success_rate = (
        sum(1 for r in results if r.fuli_success) / len(results) if results else float("nan")
    )
    error_rate = (
        sum(1 for r in results if r.winner == "both_bad") / len(results) if results else float("nan")
    )

    ci_low, ci_high = _bootstrap_ci(usable, samples=2000, seed=0)

    return {
        "schema_version": 1,
        "totals": {
            "fuli_wins": fuli_wins,
            "honcho_wins": honcho_wins,
            "ties": ties,
            "both_bad": both_bad,
            "insufficient_evidence": insufficient,
            "usable_adjudications": usable_n,
            "total": len(results),
        },
        "fuli_win_share_excluding_ties": fuli_win_share,
        "net_preference": net_preference,
        "bootstrap_95_ci": {"low": ci_low, "high": ci_high},
        "by_type": by_type,
        "by_type_net": by_type_usable,
        "contradiction_net_preference": contradiction_net,
        "temporal_net_preference": temporal_net,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "latency_max_ms": pmax,
        "fuli_success_rate": fuli_success_rate,
        "error_rate": error_rate,
    }


def _bootstrap_ci(
    results: List[AdjudicationResult],
    samples: int = 2000,
    seed: int = 0,
) -> Tuple[float, float]:
    """Bootstrap 95% CI for the net preference difference.

    Resamples the usable adjudications with replacement and
    computes the net preference on each resample. Returns the
    2.5% and 97.5% percentiles.
    """
    usable = _usable(results)
    if not usable:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    boots: List[float] = []
    for _ in range(samples):
        fuli = 0
        honcho = 0
        n = 0
        for r in rng.choices(usable, k=len(usable)):
            n += 1
            if r.winner == "fuli":
                fuli += 1
            elif r.winner == "honcho":
                honcho += 1
        if n == 0:
            continue
        boots.append((fuli - honcho) / n)
    boots.sort()
    lo = boots[int(0.025 * len(boots))]
    hi = boots[int(0.975 * len(boots))]
    return lo, hi


def canary_gate(report: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the canary quality gate to a report.

    Returns a dict of per-gate verdicts and an overall_pass bool.
    """
    totals = report["totals"]
    net = report["net_preference"]
    ci = report["bootstrap_95_ci"]
    by_type = report["by_type_net"]
    contradiction_net = report["contradiction_net_preference"]
    p95 = report["latency_p95_ms"]
    success = report["fuli_success_rate"]

    per_type_pass = True
    per_type_worst: Dict[str, float] = {}
    for qt, vals in by_type.items():
        np_val = vals["net_preference"]
        per_type_worst[qt] = np_val
        if np_val < -PER_TYPE_FLOOR_PP:
            per_type_pass = False

    overall_pass = (
        totals["usable_adjudications"] >= 100
        and ci["low"] > -NON_INFERIORITY_MARGIN_PP
        and net > -NON_INFERIORITY_MARGIN_PP
        and per_type_pass
        and (contradiction_net > -CONTRADICTION_FLOOR_PP
             if not (contradiction_net != contradiction_net) else True)
        and success >= FULI_SUCCESS_FLOOR
        and p95 <= FULI_P95_FLOOR_MS
    )

    return {
        "usable_at_least_100": totals["usable_adjudications"] >= 100,
        "ci_low_above_minus_10pp": ci["low"] > -NON_INFERIORITY_MARGIN_PP,
        "net_above_minus_10pp": net > -NON_INFERIORITY_MARGIN_PP,
        "per_type_above_minus_20pp": per_type_pass,
        "per_type_worst": per_type_worst,
        "contradiction_above_minus_10pp": (
            contradiction_net > -CONTRADICTION_FLOOR_PP
            if not (contradiction_net != contradiction_net) else True
        ),
        "fuli_success_at_least_99pct": success >= FULI_SUCCESS_FLOOR,
        "p95_latency_under_500ms": p95 <= FULI_P95_FLOOR_MS,
        "overall_pass": overall_pass,
    }
