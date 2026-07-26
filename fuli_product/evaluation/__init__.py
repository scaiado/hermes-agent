"""Evaluation package for representative quality and blind adjudication.

This package implements the Phase 6 representative quality
evaluation surface. It is split into focused modules:

- ``corpus``: representative query corpus construction and
  stratification by query type.
- ``capture``: privacy-safe capture of provider result
  fingerprints during shadow mode.
- ``replay``: deterministic replay of a captured row against a
  target provider for the blind comparison.
- ``adjudication``: blind adjudication outcomes and rationale
  codes.
- ``metrics``: per-query-type win rates, latency percentiles,
  confidence intervals, freshness, contradiction handling.
- ``report``: redacted evaluation reports for operators.

The product's top-level ``fuli_product.evaluation`` module
re-exports the public surface so existing imports keep working.
"""

from __future__ import annotations

from fuli_product.evaluation.corpus import (
    default_evaluation_corpus,
    StratifiedSampler,
)
from fuli_product.evaluation.adjudication import (
    Adjudication,
    AdjudicationOutcome,
    ReasonCode,
    REASON_CODES,
)
from fuli_product.evaluation.metrics import Metrics, QueryTypeMetrics
from fuli_product.evaluation.capture import CapturedRow, capture_row
from fuli_product.evaluation.replay import replay_row
from fuli_product.evaluation.report import build_report

__all__ = [
    "default_evaluation_corpus",
    "StratifiedSampler",
    "Adjudication",
    "AdjudicationOutcome",
    "ReasonCode",
    "REASON_CODES",
    "Metrics",
    "QueryTypeMetrics",
    "CapturedRow",
    "capture_row",
    "replay_row",
    "build_report",
]
