"""Deterministic fake used by the Reporter and integration tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class FakeComparisonRuntime:
    """In-memory stand-in for FuliComparisonRuntime.

    Implements the protocol methods consumed by the Reporter and the lifecycle:
    initialize, submit, flush, shutdown, accounting, is_balanced.
    """

    cfg: Any = None
    _initialized: bool = False
    _shutdown: bool = False
    _shutdown_calls: int = 0
    accounting_value: Dict[str, int] = field(default_factory=dict)
    submit_returns: List[Any] = field(default_factory=list)
    flush_returns: Dict[str, Any] = field(default_factory=dict)
    health_value: Dict[str, Any] = field(default_factory=dict)
    raise_on_accounting: bool = False

    def initialize(self) -> None:
        self._initialized = True

    def submit(self, query: str) -> Any:
        if self.submit_returns:
            return self.submit_returns.pop(0)
        return f"id-{query}"

    def flush(self, timeout_seconds: float = 5.0) -> Dict[str, Any]:
        return {**{"flushed": True, "timeout_seconds": timeout_seconds}, **self.flush_returns}

    def shutdown(self, drain_timeout_seconds: float = 2.0) -> Dict[str, Any]:
        self._shutdown = True
        self._shutdown_calls += 1
        return {"shutdown": True, "drain_timeout_seconds": drain_timeout_seconds}

    def accounting(self) -> Dict[str, int]:
        if self.raise_on_accounting:
            raise RuntimeError("simulated accounting failure")
        return dict(self.accounting_value)

    def is_balanced(self) -> bool:
        return True

    def health(self) -> Dict[str, Any]:
        return dict(self.health_value)


class CountingRuntime(FakeComparisonRuntime):
    """Variant that records thread interactions for leak detection."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.thread_ids: List[int] = []

    def initialize(self) -> None:
        self.thread_ids.append(threading.get_ident())
        super().initialize()