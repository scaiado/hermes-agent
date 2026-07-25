"""Adapter around the qualified comparison executor from the shadow pilot.

This module does not contain a second executor implementation. It translates
between the product's config schema and the Hermes ``pilot.comparison_executor``
API, and provides a small façade with explicit protocols so the product core
can be tested with fake providers.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from fuli_product.config import FuliProductConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class PrimaryProvider(Protocol):
    def search(self, query: str, **kwargs: Any) -> List[Any]:
        ...


@runtime_checkable
class SecondaryProvider(Protocol):
    def search(self, query: str, **kwargs: Any) -> List[Any]:
        ...


@dataclass
class ComparisonResult:
    comparison_id: str
    primary_latency_ms: float
    secondary_latency_ms: float
    primary_status: str
    secondary_status: str
    overlap_at_1: Optional[float]
    overlap_at_3: Optional[float]
    overlap_at_5: Optional[float]
    reciprocal_rank_agreement: Optional[float]
    secondary_error_category: Optional[str] = None


class FuliComparisonRuntime:
    """Product-facing runtime that wraps the qualified Hermes comparison executor.

    Provider calls are injected. The runtime does not import Fuli or Honcho
    directly; it expects callables matching ``PrimaryProvider`` / ``SecondaryProvider``.
    """

    def __init__(
        self,
        cfg: FuliProductConfig,
        *,
        primary: Callable[[str], str],
        secondary: Callable[[str], str],
        store_path: Path,
    ) -> None:
        self.cfg = cfg
        self._primary = primary
        self._secondary = secondary
        self._store_path = Path(store_path)
        self._executor: Optional[Any] = None
        self._store: Optional[Any] = None

    def initialize(self) -> None:
        from pilot.comparison_executor import ComparisonExecutor
        from pilot.comparison_store import ComparisonStore

        self._store = ComparisonStore(self._store_path)
        self._executor = ComparisonExecutor(
            max_workers=self.cfg.comparison.workers,
            max_queue_size=self.cfg.comparison.queue_size,
            comparison_budget_ms=self.cfg.comparison.budget_ms,
            secondary_call=self._secondary_call,
        )
        self._executor.start_persistence_worker(self._store)

    def _secondary_call(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Adaptor from executor's secondary_call(tool, args) to product callable(query)."""
        query = args.get("query", "")
        try:
            results = self._secondary(query)
            return json.dumps(results, default=str)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    def submit(self, query: str, *, query_type: str = "unclassified") -> Optional[str]:
        """Submit one comparison. Returns the comparison ID or None if rejected."""
        from pilot.comparison_executor import ComparisonJob
        from pilot.comparison_metrics import result_fingerprints
        from pilot.sampling import query_hash_for

        if self._executor is None:
            raise RuntimeError("runtime not initialized")

        q_hash = query_hash_for(query)
        start = time.monotonic()
        primary_raw = self._primary(query)
        primary_latency_ms = (time.monotonic() - start) * 1000.0
        primary_fps = result_fingerprints([primary_raw], limit=self.cfg.comparison.queue_size)

        comparison = ComparisonRecordPlaceholder(
            run_id=self.cfg.namespace,
            namespace=self.cfg.namespace,
            query_hash=q_hash,
            query_type=query_type,
            requested_top_k=5,
            primary_provider=self.cfg.primary_provider,
            secondary_provider=self.cfg.secondary_provider,
            primary_result_fingerprints=primary_fps,
            primary_latency_ms=primary_latency_ms,
            secondary_search_args={"query": query, "mode": "hybrid"},
        )
        job = ComparisonJob(
            job_id=comparison.comparison_id,
            comparison=comparison,
            secondary_search_args={"query": query, "mode": "hybrid"},
            primary_result_fingerprints=primary_fps,
            primary_provider=self.cfg.primary_provider,
            primary_status="success",
            primary_latency_ms=primary_latency_ms,
            query_hash=q_hash,
            decision_bucket=0,
            enqueued_at_monotonic=time.monotonic(),
        )
        accepted = self._executor.enqueue(job)
        return comparison.comparison_id if accepted else None

    def flush(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        if self._executor is None:
            return {"flushed": True, "executor_accounting": {}}
        return self._executor.flush(timeout_seconds=timeout_seconds)

    def shutdown(self, drain_timeout_seconds: float = 10.0) -> Dict[str, Any]:
        if self._executor is None:
            return {"already_stopped": True, "executor_accounting": {}}
        return self._executor.shutdown(drain_timeout_seconds=drain_timeout_seconds)

    def accounting(self) -> Dict[str, Any]:
        if self._executor is None:
            return {}
        return self._executor.accounting()

    def is_balanced(self) -> bool:
        if self._executor is None:
            return True
        return self._executor.is_balanced()


@dataclass
class ComparisonRecordPlaceholder:
    """Minimal in-memory ComparisonRecord-like object for the qualified executor.

    The qualified executor reads these fields from ``job.comparison``. We do not
    import the real ComparisonRecord here to keep ``fuli_product`` core free of
    the full pilot schema at import time.
    """

    run_id: str
    namespace: str
    query_hash: str
    query_type: str = "unclassified"
    requested_top_k: int = 5
    primary_provider: str = "honcho"
    secondary_provider: str = "fuli"
    primary_latency_ms: float = 0.0
    secondary_latency_ms: float = 0.0
    primary_status: str = ""
    secondary_status: str = ""
    primary_error_category: Optional[str] = None
    secondary_error_category: Optional[str] = None
    primary_result_fingerprints: List[str] = field(default_factory=list)
    secondary_result_fingerprints: List[str] = field(default_factory=list)
    overlap_at_1: Optional[float] = None
    overlap_at_3: Optional[float] = None
    overlap_at_5: Optional[float] = None
    reciprocal_rank_agreement: Optional[float] = None
    missing_from_primary: List[str] = field(default_factory=list)
    missing_from_secondary: List[str] = field(default_factory=list)
    secondary_retrieval_mode: str = "unknown"
    content_captured: bool = False
    adjudication_status: str = "pending"
    schema_version: int = 1
    comparison_id: str = ""
    timestamp: str = ""
    secondary_search_args: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.comparison_id:
            self.comparison_id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
