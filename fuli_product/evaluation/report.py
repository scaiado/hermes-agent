"""Redacted evaluation report builder.

The report is the operator-facing output of the evaluation pipeline.
It contains no raw query text, no raw provider payloads, no
configuration secrets, and no environment variables. All fields
are summaries and fingerprints.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fuli_product.evaluation.adjudication import Adjudication, summarize
from fuli_product.evaluation.metrics import Metrics, QueryTypeMetrics


def build_report(
    *,
    metrics: Metrics,
    adjudications: List[Adjudication],
    p95_threshold_ms: float = 1500.0,
    fuli_success_floor: float = 0.99,
    fuli_win_floor: float = 0.45,
    n_min: int = 100,
    period: str = "adjudicated_shadow",
) -> Dict[str, Any]:
    """Build a redacted evaluation report.

    The output is JSON-serializable and contains:
      - schema_version
      - generated_at
      - period
      - overall: QueryTypeMetrics
      - by_type: dict[str, QueryTypeMetrics]
      - adjudication_summary: dict[str, int]
      - canary_gate: dict[str, Any]
    """
    by_type = {qt: asdict(m) for qt, m in metrics.by_type.items()}
    gate = metrics.canary_gate(
        p95_threshold_ms=p95_threshold_ms,
        fuli_success_floor=fuli_success_floor,
        fuli_win_floor=fuli_win_floor,
        n_min=n_min,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": period,
        "overall": asdict(metrics.overall),
        "by_type": by_type,
        "adjudication_summary": summarize(adjudications),
        "canary_gate": gate,
    }


def write_report(report: Dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return output
