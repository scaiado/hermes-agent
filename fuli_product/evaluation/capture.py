"""Privacy-safe capture of provider result fingerprints during shadow.

The capture layer is invoked from the shadow mode comparison pipeline.
It produces a ``CapturedRow`` containing only fingerprints and the
minimum required metadata. Raw provider response text and raw query
text are NEVER persisted. The query is reduced to a SHA-256 prefix
for the comparison_id; the full query text is kept in-memory only
to drive the replay phase and is never written to the captured row.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _fp(payload: Any) -> str:
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:16]


@dataclass
class CapturedRow:
    """A single privacy-safe shadow comparison row.

    Fields:
      - comparison_id: short hash identifying this row.
      - query_hash: SHA-256 prefix of the query text (16 hex chars).
      - query_type: one of REQUIRED_QUERY_TYPES.
      - provider_fingerprints: list of 16-char hex fingerprints,
        one per primary result.
      - primary_provider, secondary_provider: provider names.
      - retrieval_mode: "hybrid" / "semantic" / "exact" / etc.
      - primary_latency_ms, secondary_latency_ms.
      - namespace.
      - captured_at: ISO-8601 UTC timestamp.
      - adjudication: optional blind adjudication outcome.
    """

    comparison_id: str
    query_hash: str
    query_type: str
    provider_fingerprints: List[str]
    primary_provider: str
    secondary_provider: str
    retrieval_mode: str
    primary_latency_ms: float
    secondary_latency_ms: float
    namespace: str
    captured_at: str
    freshness_metadata: Dict[str, Any] = field(default_factory=dict)
    blind_candidate_ordering: List[str] = field(default_factory=list)
    adjudication: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "query_hash": self.query_hash,
            "query_type": self.query_type,
            "provider_fingerprints": list(self.provider_fingerprints),
            "primary_provider": self.primary_provider,
            "secondary_provider": self.secondary_provider,
            "retrieval_mode": self.retrieval_mode,
            "primary_latency_ms": self.primary_latency_ms,
            "secondary_latency_ms": self.secondary_latency_ms,
            "namespace": self.namespace,
            "captured_at": self.captured_at,
            "freshness_metadata": dict(self.freshness_metadata),
            "blind_candidate_ordering": list(self.blind_candidate_ordering),
            "adjudication": dict(self.adjudication) if self.adjudication else None,
        }


def capture_row(
    *,
    query: str,
    query_type: str,
    primary_payload: Any,
    secondary_payload: Any,
    primary_provider: str,
    secondary_provider: str,
    retrieval_mode: str = "hybrid",
    namespace: str = "hermes:default",
    primary_latency_ms: float = 0.0,
    secondary_latency_ms: float = 0.0,
    freshness_metadata: Optional[Dict[str, Any]] = None,
    blind_candidate_ordering: Optional[List[str]] = None,
) -> CapturedRow:
    """Build a CapturedRow with only fingerprints and metadata.

    The raw query text is hashed once and not retained; the raw
    primary/secondary payloads are reduced to 16-char hex
    fingerprints.
    """
    q_hash = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:16]
    primary_fps = [_fp(x) for x in _extract_items(primary_payload)]
    secondary_fps = [_fp(x) for x in _extract_items(secondary_payload)]
    blind = list(blind_candidate_ordering) if blind_candidate_ordering else (
        primary_fps + secondary_fps
    )
    return CapturedRow(
        comparison_id=hashlib.sha256(
            f"{q_hash}:{primary_provider}:{secondary_provider}:{retrieval_mode}".encode("utf-8")
        ).hexdigest()[:16],
        query_hash=q_hash,
        query_type=query_type,
        provider_fingerprints=primary_fps + secondary_fps,
        primary_provider=primary_provider,
        secondary_provider=secondary_provider,
        retrieval_mode=retrieval_mode,
        primary_latency_ms=primary_latency_ms,
        secondary_latency_ms=secondary_latency_ms,
        namespace=namespace,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        freshness_metadata=dict(freshness_metadata or {}),
        blind_candidate_ordering=blind,
    )


def _extract_items(payload: Any) -> List[Any]:
    if isinstance(payload, dict):
        return [payload] if payload else []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        try:
            import json
            obj = json.loads(payload)
            return _extract_items(obj)
        except Exception:
            return [payload]
    return [payload]
