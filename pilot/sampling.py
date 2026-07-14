"""Deterministic seed-based sampling for the shadow pilot's read-comparison path.

The shadow provider must decide, on every read, whether to compare the
primary's result against the secondary's. We want:

- Determinism: a given (sampling_seed, query_hash, namespace) tuple always
  produces the same decision, so reruns are reproducible.
- Independence: different seeds yield different sample sets.
- Boundedness: a sample_rate of 0.0 disables; 1.0 forces; values in
  between select a stable hash-bucket subset.

The hashing is on a 32-bit bucket space (0..2**32 - 1). ``sample_rate``
selects the lowest ``sample_rate`` fraction of that space.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class SamplingDecision:
    """Result of ``should_sample`` — records everything needed to reproduce."""

    sample: bool
    bucket: int
    bucket_space: int
    threshold: int
    sampling_seed: int
    query_hash: str
    namespace: str

    def to_dict(self) -> dict:
        return {
            "sample": self.sample,
            "bucket": self.bucket,
            "bucket_space": self.bucket_space,
            "threshold": self.threshold,
            "sampling_seed": self.sampling_seed,
            "query_hash": self.query_hash,
            "namespace": self.namespace,
        }


def _stable_bucket(seed: int, query_hash: str, namespace: str, bucket_space: int) -> int:
    """Map (seed, query, namespace) to a stable integer in [0, bucket_space).

    Uses SHA-256 over a canonical string. The first 8 hex chars give 32 bits
    of entropy, more than enough for a 1-in-N sample decision.
    """
    canonical = f"{int(seed)}|{query_hash}|{namespace}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % bucket_space


def should_sample(
    *,
    sample_rate: float,
    sampling_seed: int,
    query_hash: str,
    namespace: str,
    bucket_space: int = 2**32,
) -> SamplingDecision:
    """Return a deterministic SamplingDecision.

    - ``sample_rate <= 0`` → never sample.
    - ``sample_rate >= 1`` → always sample.
    - Otherwise, sample iff bucket < sample_rate * bucket_space.
    """
    if sample_rate <= 0:
        return SamplingDecision(
            sample=False,
            bucket=0,
            bucket_space=bucket_space,
            threshold=0,
            sampling_seed=sampling_seed,
            query_hash=query_hash,
            namespace=namespace,
        )
    if sample_rate >= 1:
        return SamplingDecision(
            sample=True,
            bucket=0,
            bucket_space=bucket_space,
            threshold=bucket_space,
            sampling_seed=sampling_seed,
            query_hash=query_hash,
            namespace=namespace,
        )
    bucket = _stable_bucket(sampling_seed, query_hash, namespace, bucket_space)
    threshold = int(sample_rate * bucket_space)
    return SamplingDecision(
        sample=bucket < threshold,
        bucket=bucket,
        bucket_space=bucket_space,
        threshold=threshold,
        sampling_seed=sampling_seed,
        query_hash=query_hash,
        namespace=namespace,
    )


def query_hash_for(query: str) -> str:
    """SHA-256[:16] hex of a query string."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


__all__ = ["SamplingDecision", "should_sample", "query_hash_for"]