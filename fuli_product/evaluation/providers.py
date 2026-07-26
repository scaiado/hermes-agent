"""Provider adapters for the Phase 6 evaluation benchmark.

The benchmark harness runs the same approved corpus against each
provider adapter. The adapters implement a common protocol
(`retain`, `recall`, `delete/reset`, `health`, `capabilities`,
`latency`, `provenance`) so the harness is provider-agnostic.

The adapters in this package are for evaluation only. They are
NOT loaded by the production runtime; production routing uses
`fuli_product.router.CanaryRouter` plus the qualified shadow
plugin. Hindsight and Mnemosyne adapters ship as optional
scaffolding that activates only when the corresponding
provider package is importable; the harness reports "absent"
when the package is missing and continues with the providers
that are present.
"""

from __future__ import annotations

import importlib.util
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Set


@dataclass
class Capabilities:
    """Static capability declaration of a provider adapter."""

    retain: bool = True
    recall: bool = True
    delete: bool = True
    health: bool = True
    retrieval_modes: Set[str] = field(default_factory=lambda: {"hybrid"})
    typed_schema: bool = False
    graph_links: bool = False
    contradiction_detection: bool = False
    embedded: bool = True
    server_mode: bool = False


@dataclass
class RecallResult:
    fingerprints: List[str]
    latency_ms: float
    retrieval_mode: str
    provenance: Dict[str, Any]
    raw_count: int = 0


class ProviderAdapter(Protocol):
    """Common interface for benchmark adapters."""

    name: str

    def retain(self, query: str, query_type: str, metadata: Dict[str, Any]) -> str: ...
    def recall(self, query: str, top_k: int) -> RecallResult: ...
    def delete_test_namespace(self) -> None: ...
    def health(self) -> bool: ...
    def capabilities(self) -> Capabilities: ...
    def latency(self) -> float: ...
    def provenance(self) -> Dict[str, str]: ...


def _fingerprint(payload: Any) -> str:
    import hashlib
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:16]


# --- Honcho adapter (always available) ----------------------------------


class HonchoAdapter:
    """Honcho adapter stub. Operates on a thin in-memory dict; the
    real adapter will use the Honcho SDK when the harness connects
    to a live Honcho endpoint.

    This stub is sufficient for benchmark dry-runs and for proving
    the adapter protocol is correct.
    """

    name = "honcho"

    def __init__(self) -> None:
        self._store: Dict[str, List[Dict[str, Any]]] = {}

    def retain(self, query: str, query_type: str, metadata: Dict[str, Any]) -> str:
        fp = _fingerprint({"q": query, "t": query_type, "m": metadata})
        self._store.setdefault(query_type, []).append({
            "fp": fp, "query": query, "metadata": metadata,
        })
        return fp

    def recall(self, query: str, top_k: int) -> RecallResult:
        t0 = time.monotonic()
        qf = _fingerprint({"q": query})
        candidates = []
        for qt, rows in self._store.items():
            for r in rows:
                if r["fp"] == qf:
                    candidates.append((qt, r))
        candidates = candidates[:top_k]
        latency = (time.monotonic() - t0) * 1000.0
        return RecallResult(
            fingerprints=[c[1]["fp"] for c in candidates],
            latency_ms=latency,
            retrieval_mode="hybrid",
            provenance={"provider": "honcho", "store": "in-memory"},
            raw_count=len(candidates),
        )

    def delete_test_namespace(self) -> None:
        self._store.clear()

    def health(self) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            retain=True, recall=True, delete=True, health=True,
            retrieval_modes={"hybrid"}, typed_schema=False,
            graph_links=False, contradiction_detection=False,
            embedded=True, server_mode=False,
        )

    def latency(self) -> float:
        return 5.0

    def provenance(self) -> Dict[str, str]:
        return {"provider": "honcho", "version": "stub"}


# --- Fuli adapter --------------------------------------------------------


class FuliAdapter:
    """Fuli adapter. Calls the Fuli provider via the secondary_call
    wrapper used by the qualified pilot, with the same fingerprint
    contract as Honcho. Operates in-memory; the real adapter will
    use the Fuli-Memory-Core sidecar."""

    name = "fuli"

    def __init__(self) -> None:
        self._store: Dict[str, List[Dict[str, Any]]] = {}

    def retain(self, query: str, query_type: str, metadata: Dict[str, Any]) -> str:
        fp = _fingerprint({"q": query, "t": query_type, "m": metadata})
        self._store.setdefault(query_type, []).append({
            "fp": fp, "query": query, "metadata": metadata,
        })
        return fp

    def recall(self, query: str, top_k: int) -> RecallResult:
        t0 = time.monotonic()
        # Naive intersection; the real Fuli call would invoke the
        # qualified pilot's secondary_call wrapper.
        qf = _fingerprint({"q": query})
        candidates = []
        for qt, rows in self._store.items():
            for r in rows:
                if r["fp"] == qf:
                    candidates.append((qt, r))
        candidates = candidates[:top_k]
        latency = (time.monotonic() - t0) * 1000.0
        return RecallResult(
            fingerprints=[c[1]["fp"] for c in candidates],
            latency_ms=latency,
            retrieval_mode="hybrid",
            provenance={"provider": "fuli", "store": "in-memory"},
            raw_count=len(candidates),
        )

    def delete_test_namespace(self) -> None:
        self._store.clear()

    def health(self) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            retain=True, recall=True, delete=True, health=True,
            retrieval_modes={"hybrid", "semantic", "exact"},
            typed_schema=True, graph_links=False,
            contradiction_detection=True, embedded=True,
            server_mode=False,
        )

    def latency(self) -> float:
        return 8.0

    def provenance(self) -> Dict[str, str]:
        return {"provider": "fuli", "version": "stub"}


# --- Hindsight / Mnemosyne stubs (only active when packages present) ----


def make_hindsight_adapter() -> Optional[ProviderAdapter]:
    """Return a Hindsight adapter if the Hindsight package is
    importable; None otherwise. The benchmark harness treats None
    as "absent" and continues with the providers that are present.
    """
    spec = importlib.util.find_spec("hindsight")
    if spec is None:
        return None
    # The real adapter would import hindsight and call its retain/recall
    # APIs. We keep the import optional; when Hindsight is present,
    # the operator should replace this stub with the real one.
    class _HindsightStub:
        name = "hindsight"

        def retain(self, query: str, query_type: str, metadata: Dict[str, Any]) -> str:
            return _fingerprint({"q": query, "t": query_type, "m": metadata})

        def recall(self, query: str, top_k: int) -> RecallResult:
            return RecallResult(
                fingerprints=[], latency_ms=0.0, retrieval_mode="hybrid",
                provenance={"provider": "hindsight", "note": "stub"},
            )

        def delete_test_namespace(self) -> None:
            pass

        def health(self) -> bool:
            return False  # stub; real adapter returns True

        def capabilities(self) -> Capabilities:
            return Capabilities(
                retain=True, recall=True, delete=True, health=True,
                retrieval_modes={"hybrid", "semantic", "keyword", "graph", "temporal"},
                typed_schema=True, graph_links=True,
                contradiction_detection=True, embedded=True,
                server_mode=True,
            )

        def latency(self) -> float:
            return 12.0

        def provenance(self) -> Dict[str, str]:
            return {"provider": "hindsight", "version": "stub"}
    return _HindsightStub()


def make_mnemosyne_adapter() -> Optional[ProviderAdapter]:
    """Return a Mnemosyne adapter if the Mnemosyne package is
    importable; None otherwise. The real adapter (when Mnemosyne is
    present) would use its retain/recall APIs and surface its
    capabilities; we keep the import optional."""
    spec = importlib.util.find_spec("mnemosyne")
    if spec is None:
        return None
    class _MnemosyneStub:
        name = "mnemosyne"

        def retain(self, query: str, query_type: str, metadata: Dict[str, Any]) -> str:
            return _fingerprint({"q": query, "t": query_type, "m": metadata})

        def recall(self, query: str, top_k: int) -> RecallResult:
            return RecallResult(
                fingerprints=[], latency_ms=0.0, retrieval_mode="hybrid",
                provenance={"provider": "mnemosyne", "note": "stub"},
            )

        def delete_test_namespace(self) -> None:
            pass

        def health(self) -> bool:
            return False  # stub; real adapter returns True

        def capabilities(self) -> Capabilities:
            return Capabilities(
                retain=True, recall=True, delete=True, health=True,
                retrieval_modes={"hybrid", "semantic", "keyword"},
                typed_schema=True, graph_links=True,
                contradiction_detection=True, embedded=True,
                server_mode=False,
            )

        def latency(self) -> float:
            return 10.0

        def provenance(self) -> Dict[str, str]:
            return {"provider": "mnemosyne", "version": "stub"}
    return _MnemosyneStub()


# --- harness helper ------------------------------------------------------


def all_available_adapters() -> Dict[str, Optional[ProviderAdapter]]:
    """Return every adapter the benchmark harness can attempt to use.

    Honcho and Fuli are always present. Hindsight and Mnemosyne are
    present only when the corresponding package is importable.
    """
    return {
        "honcho": HonchoAdapter(),
        "fuli": FuliAdapter(),
        "hindsight": make_hindsight_adapter(),
        "mnemosyne": make_mnemosyne_adapter(),
    }
