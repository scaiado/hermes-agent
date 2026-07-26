"""Deterministic replay of a captured row against a target provider.

The replay layer is used by the blind adjudication workflow. A
captured row contains only fingerprints and metadata; replay
takes the original query (passed in-memory by the operator at
replay time, never persisted) and the captured row, invokes the
target provider, and returns the new provider's fingerprints and
latency. The replay output is fingerprinted only — the raw
response is not stored.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Dict, List

from fuli_product.evaluation.capture import CapturedRow, _fp, _extract_items


def replay_row(
    row: CapturedRow,
    *,
    query: str,
    target_provider: str,
    call: Callable[[str], Any],
    retrieval_mode: str = "hybrid",
) -> Dict[str, Any]:
    """Invoke ``call(query)`` as ``target_provider``; return a privacy-
    safe replay summary.

    The query is held in memory only. The target provider's response
    is reduced to a fingerprint list. The original captured row's
    fingerprints are not modified.
    """
    t0 = time.monotonic()
    raw = call(query)
    latency_ms = (time.monotonic() - t0) * 1000.0
    new_fps = [_fp(x) for x in _extract_items(raw)]
    return {
        "comparison_id": hashlib.sha256(
            f"{row.comparison_id}:{target_provider}:{retrieval_mode}".encode("utf-8")
        ).hexdigest()[:16],
        "source_capture_id": row.comparison_id,
        "target_provider": target_provider,
        "retrieval_mode": retrieval_mode,
        "provider_fingerprints": new_fps,
        "latency_ms": latency_ms,
        "replayed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
