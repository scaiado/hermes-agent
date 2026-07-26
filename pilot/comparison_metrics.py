"""Comparison metrics for the Hermes ↔ Fuli shadow pilot.

Pure functions for the deterministic, redaction-safe comparison metrics used
by the sampled-read evidence pipeline. Nothing here stores raw content,
provider-specific IDs, or anything else that could leak private data.

Fingerprints
------------

A "fingerprint" is a SHA-256 hex digest of a canonicalized redacted
description of a search result, not the raw text. The canonical form strips
provider-specific identifiers (Honcho message IDs, Fuli ULIDs), normalizes
whitespace, and lowercases. This means:

- The same memory text indexed by both providers produces the same fingerprint.
- Provider IDs do not affect the fingerprint (test requirement #7).
- Fingerprints are stable across runs and across machines.

Overlap@k
---------

``overlap_at_k(primary_fps, secondary_fps, k)`` returns the fraction of
the first ``k`` primary fingerprints that also appear in the first ``k``
secondary fingerprints. Returns ``None`` when primary has no top-k results,
matching the previous pilot behavior so existing reports remain comparable.

Reciprocal-rank agreement
-------------------------

``reciprocal_rank_agreement(primary_fps, secondary_fps)`` returns a value in
[0, 1] representing how closely the two rankings agree. The first
agreement fingerprint is the highest-ranked primary result; its rank in
the secondary list determines the score: 1/(rank+1) if present, 0
otherwise. We average the reciprocal ranks across all primary results to
produce a symmetric agreement signal. A perfect match returns 1.0;
complete disagreement returns 0.0.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Optional, Sequence


# Fields that frequently carry provider-specific identifiers. Removing them
# from the canonical form keeps fingerprints stable across providers.
_PROVIDER_ID_KEYS = {
    "memory_id",
    "id",
    "message_id",
    "session_id",
    "peer_id",
    "workspace_id",
    "conclusion_id",
    "observation_id",
    "ep_id",
    "ulid",
    "uuid",
}


def normalize_for_fingerprint(value: str) -> str:
    """Return a redacted, whitespace-normalized, lowercase form of ``value``.

    Strips JSON braces/quotes, collapses whitespace, removes common
    provider-id-like tokens. Never raises; falls back to the input as-is.
    """
    if not value:
        return ""
    text = value
    # Drop JSON-like braces/quotes that often wrap provider output.
    text = re.sub(r"[{}\"'\[\],]", " ", text)
    # Drop UUID/ULID-shaped tokens (8-4-4-4-12 hex pattern, or 26-char base32).
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", " ", text)  # Crockford ULID-ish
    # Strip all non-alphanumeric, non-space characters so punctuation and
    # provider-specific noise (semicolons, slashes, brackets) cannot
    # differentiate fingerprints.
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    # Collapse whitespace and trim.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def result_fingerprint(result: object) -> str:
    """Compute a stable fingerprint from a result object.

    Accepts strings (treated as raw text), dicts (canonicalize their
    textual fields), None (treated as empty), or anything else (converted
    to string). Two inputs that normalize to the same canonical form
    produce the same fingerprint.
    """
    if result is None:
        canonical = ""
    elif isinstance(result, str):
        canonical = normalize_for_fingerprint(result)
    elif isinstance(result, dict):
        parts: list[str] = []
        for k, v in result.items():
            if k.lower() in _PROVIDER_ID_KEYS:
                continue
            if isinstance(v, (str, int, float)):
                parts.append(str(v))
            elif v is None:
                continue
            else:
                parts.append(repr(v))
        canonical = normalize_for_fingerprint(" ".join(parts)) if parts else ""
    else:
        canonical = normalize_for_fingerprint(str(result))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def result_fingerprints(results: Optional[Iterable[object]], limit: Optional[int] = None) -> List[str]:
    """Fingerprint each result in order. Optional cap to avoid bloat."""
    out: List[str] = []
    if results is None:
        return out
    for item in results:
        out.append(result_fingerprint(item))
        if limit is not None and len(out) >= limit:
            break
    return out


def overlap_at_k(
    primary: Sequence[str],
    secondary: Sequence[str],
    k: int,
) -> Optional[float]:
    """Fraction of primary's top-k that also appear in secondary's top-k.

    Returns ``None`` when either side's top-k is empty (consistent with
    the "empty-result rate is reported separately" semantics; a missing
    comparison is not the same as a zero-overlap comparison).
    """
    if k <= 0:
        return None
    p_set = set(list(primary)[:k])
    s_set = set(list(secondary)[:k])
    if not p_set or not s_set:
        return None
    return len(p_set & s_set) / len(p_set)


def reciprocal_rank_agreement(
    primary: Sequence[str],
    secondary: Sequence[str],
) -> float:
    """Mean Reciprocal Rank (MRR) agreement in [0, 1].

    For each ranking, find the position of the *first* matching item in
    the other ranking. Reciprocal rank is 1/(position); if there is no
    match, the contribution is 0. The forward MRR uses ``primary`` as the
    query and ``secondary`` as the key (i.e. "is the first item in
    primary found in secondary, and at what rank?"), the backward MRR
    swaps the two. The returned value is the symmetric mean.

    With a perfect match (identical orderings), the forward and
    backward MRR are both 1.0, so the symmetric mean is 1.0.
    """
    if not primary or not secondary:
        return 0.0

    def _mrr(query: Sequence[str], key: Sequence[str]) -> float:
        key_index = {item: idx + 1 for idx, item in enumerate(key)}
        for item in query:
            rank = key_index.get(item)
            if rank is not None:
                return 1.0 / rank
        return 0.0

    forward = _mrr(primary, secondary)
    backward = _mrr(secondary, primary)
    return (forward + backward) / 2.0


def missing_from(
    superset_fps: Sequence[str],
    subset_fps: Sequence[str],
    k: int,
) -> List[str]:
    """Top-k fingerprints of ``superset`` not present in ``subset``.

    Used to surface "primary-only" or "secondary-only" entries for adjudication.
    """
    sub = set(subset_fps)
    return [fp for fp in list(superset_fps)[:k] if fp not in sub]


__all__ = [
    "normalize_for_fingerprint",
    "result_fingerprint",
    "result_fingerprints",
    "overlap_at_k",
    "reciprocal_rank_agreement",
    "missing_from",
]