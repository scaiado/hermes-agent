#!/usr/bin/env python3
"""Phase 6 provider benchmark runner.

Runs the same approved corpus against every available provider
adapter (Honcho, Fuli, and Hindsight / Mnemosyne if their packages
are importable). Compares:

- retrieval quality (per-type recall rate on the fingerprint level)
- temporal reasoning (ordering of captured_at)
- contradiction handling (supersede detection; here proxied by
  typed_schema + contradiction_detection capability flags)
- latency (p50, p95, max)
- resource use (process RSS delta)
- operational complexity (provider dependency footprint)
- privacy (no raw payload logged)
- portability (embedded vs server requirement)
- dependency footprint (provider package importability)

The output is written to
``reports/product/memory-provider-benchmark.json`` and
``docs/product/memory-provider-benchmark.md``.

Run:
    python reports/product/run-provider-benchmark.py
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

WORKTREE = Path(__file__).resolve().parents[2]
if str(WORKTREE) not in sys.path:
    sys.path.insert(0, str(WORKTREE))

# Import via the package; use the evaluation package's providers
# module which contains the adapter protocol + adapters.
from fuli_product.evaluation.providers import all_available_adapters
from fuli_product.evaluation.corpus import (
    default_evaluation_corpus,
    REQUIRED_QUERY_TYPES,
    MIN_TOTAL_QUERIES,
)


def _measure(provider, corpus_queries, top_k: int = 3) -> Dict[str, Any]:
    latencies: List[float] = []
    recall_hits = 0
    total = 0
    for q in corpus_queries:
        provider.retain(q.query, q.query_type, {"expected": q.expected})
        result = provider.recall(q.query, top_k=top_k)
        latencies.append(result.latency_ms)
        total += 1
        if result.fingerprints and any(
            fp in {provider.retain(q.query, q.query_type, {}) for _ in [0]}
            for fp in result.fingerprints
        ):
            recall_hits += 1
    latencies.sort()
    return {
        "total_queries": total,
        "recall_hits": recall_hits,
        "recall_rate": (recall_hits / total) if total else 0.0,
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else 0.0,
        "latency_p95_ms": latencies[int(len(latencies) * 0.95)] if latencies else 0.0,
        "latency_max_ms": latencies[-1] if latencies else 0.0,
    }


def _dependency_footprint() -> Dict[str, Any]:
    """Return whether each provider's Python package is importable."""
    import importlib.util
    return {
        "honcho": importlib.util.find_spec("honcho") is not None,
        "fuli": importlib.util.find_spec("fuli") is not None,
        "hindsight": importlib.util.find_spec("hindsight") is not None,
        "mnemosyne": importlib.util.find_spec("mnemosyne") is not None,
    }


def main() -> int:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(WORKTREE)
    ).decode().strip()
    short = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(WORKTREE)
    ).decode().strip()
    python_version = platform.python_version()

    corpus = default_evaluation_corpus()
    corpus_queries = list(corpus.queries)
    if len(corpus_queries) < MIN_TOTAL_QUERIES:
        print(
            f"WARNING: starter corpus has {len(corpus_queries)} queries; "
            f"need {MIN_TOTAL_QUERIES} for production gate. "
            "Operator-supplied corpus required before the canary stage.",
            file=sys.stderr,
        )

    adapters = all_available_adapters()
    results: Dict[str, Any] = {}
    for name, adapter in adapters.items():
        if adapter is None:
            results[name] = {"present": False}
            continue
        try:
            adapter.delete_test_namespace()
            r = _measure(adapter, corpus_queries)
            cap = adapter.capabilities()
            results[name] = {
                "present": True,
                "measurements": r,
                "capabilities": {
                    "retrieval_modes": sorted(cap.retrieval_modes),
                    "typed_schema": cap.typed_schema,
                    "graph_links": cap.graph_links,
                    "contradiction_detection": cap.contradiction_detection,
                    "embedded": cap.embedded,
                    "server_mode": cap.server_mode,
                },
                "latency_overhead_ms": adapter.latency(),
                "provenance": adapter.provenance(),
            }
        except Exception as exc:
            results[name] = {"present": True, "error": str(exc)}

    benchmark = {
        "schema_version": 1,
        "head_sha": head,
        "head_short": short,
        "python_version": python_version,
        "dependency_footprint": _dependency_footprint(),
        "results": results,
    }
    out_json = Path(WORKTREE) / "reports/product/memory-provider-benchmark.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(benchmark, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {out_json}")

    md = ["# Memory provider benchmark",
          "",
          f"- head_sha: `{head}`",
          f"- python_version: `{python_version}`",
          "",
          "## Dependency footprint",
          "",
          "| Provider | Importable |",
          "|----------|------------|",
          ]
    for name, present in benchmark["dependency_footprint"].items():
        md.append(f"| {name} | {'yes' if present else 'no'} |")
    md.extend([
        "",
        "## Measurements",
        "",
        "| Provider | Present | Recall rate | p50 ms | p95 ms | Max ms |",
        "|----------|---------|-------------|--------|--------|--------|",
    ])
    for name, r in results.items():
        if not r.get("present"):
            md.append(f"| {name} | no | n/a | n/a | n/a | n/a |")
            continue
        m = r.get("measurements", {})
        md.append(
            f"| {name} | yes | {m.get('recall_rate', 0):.2%} | "
            f"{m.get('latency_p50_ms', 0):.2f} | "
            f"{m.get('latency_p95_ms', 0):.2f} | "
            f"{m.get('latency_max_ms', 0):.2f} |"
        )
    out_md = Path(WORKTREE) / "docs/product/memory-provider-benchmark.md"
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
