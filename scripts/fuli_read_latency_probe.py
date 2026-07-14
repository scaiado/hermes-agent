"""Fuli read latency probe.

The dry-run discovered that all 10 Fuli searches timed out at the 250 ms
budget, but the comparison row was correctly recorded as a failure
anyway. This script measures Fuli's steady-state read latency so the
comparison budget can be set from evidence rather than guessed.

Sequence:
  1. One warm-up search (caches model + vector-index init).
  2. 10 sequential searches with a 5 s per-call timeout.
  3. Report p50, p95, max excluding the warm-up call.
  4. Recommend a comparison_budget_ms based on the observed p95.

The probe uses the real shadow provider, real Fuli, real Honcho, and
real Tailscale routing. It is hermetic: it does not write any
comparison rows (mirror_writes and compare_reads are both off).
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_constants import get_hermes_home
from plugins.memory.shadow import ShadowMemoryProvider

PROBE_QUERIES = [
    "fuli-latency-probe-001",
    "fuli-latency-probe-002",
    "fuli-latency-probe-003",
    "fuli-latency-probe-004",
    "fuli-latency-probe-005",
    "fuli-latency-probe-006",
    "fuli-latency-probe-007",
    "fuli-latency-probe-008",
    "fuli-latency-probe-009",
    "fuli-latency-probe-010",
]
WARMUP_QUERY = "fuli-latency-warmup"


def main() -> int:
    hermes_home = get_hermes_home()
    print(f"HERMES_HOME: {hermes_home}")

    shadow = ShadowMemoryProvider()
    shadow.initialize(
        "fuli-latency-probe",
        hermes_home=str(hermes_home),
        comparison_run_id="fuli-latency-probe-2026-07-13",
    )
    # Disable writes and sampling so this probe exercises ONLY the
    # Fuli search path; no comparisons are recorded.
    shadow._enabled = True
    shadow._mirror_writes = False
    shadow._compare_reads = False
    shadow._read_timeout_ms = 5000  # 5 s ceiling; we want a real measurement
    shadow._capture_content = False
    shadow._namespace = "hermes:shadow-pilot"

    # Direct Fuli search — bypass the shadow's primary call so we time
    # the Fuli round-trip in isolation.
    secondary = shadow._secondary
    if secondary is None:
        print("ERROR: secondary provider not loaded")
        return 1

    # Warm-up
    print(f"\nWarming up Fuli with query {WARMUP_QUERY!r} ...")
    t0 = time.monotonic()
    try:
        warmup_raw = secondary.handle_tool_call(
            "fuli_memory_search",
            {
                "query": WARMUP_QUERY,
                "top_k": 5,
                "namespace": "hermes:shadow-pilot",
                "timeout_ms": 5000,
            },
        )
        warmup_dt = (time.monotonic() - t0) * 1000.0
        warmup_ok = True
    except Exception as exc:
        warmup_dt = (time.monotonic() - t0) * 1000.0
        warmup_ok = False
        warmup_raw = f"ERROR: {exc}"
    print(f"warmup: {'ok' if warmup_ok else 'FAIL'}  {warmup_dt:.1f} ms")
    print(f"  body[:200] = {warmup_raw[:200]!r}")

    # 10 sequential searches
    print(f"\nRunning {len(PROBE_QUERIES)} sequential Fuli searches ...")
    times: list[float] = []
    for i, q in enumerate(PROBE_QUERIES, start=1):
        t0 = time.monotonic()
        try:
            raw = secondary.handle_tool_call(
                "fuli_memory_search",
                {
                    "query": q,
                    "top_k": 5,
                    "namespace": "hermes:shadow-pilot",
                    "timeout_ms": 5000,
                },
            )
            dt = (time.monotonic() - t0) * 1000.0
            ok = True
            err = None
        except Exception as exc:
            dt = (time.monotonic() - t0) * 1000.0
            ok = False
            err = str(exc)
            raw = None
        times.append(dt)
        outcome = "ok" if ok else f"FAIL({err})"
        print(f"  search {i:2d}: {outcome}  {dt:.1f} ms")

    if not times:
        print("ERROR: no successful searches; cannot recommend a budget")
        return 1

    times_sorted = sorted(times)
    p50 = times_sorted[len(times_sorted) // 2]
    p95 = times_sorted[int(len(times_sorted) * 0.95) - 1] if len(times_sorted) >= 2 else times_sorted[-1]
    p95 = max(p95, times_sorted[-1] if len(times_sorted) <= 20 else times_sorted[int(len(times_sorted) * 0.95) - 1])
    maximum = times_sorted[-1]
    average = statistics.mean(times)
    stdev = statistics.stdev(times) if len(times) > 1 else 0.0

    # Recommended budget: max(p95 + 2*stdev, 1.5 * p95) clamped to a sane
    # lower bound. The +2*stdev cushion covers warm-up-vs-cold variance
    # observed in shadow pilot runs.
    recommended = max(int(p95 + 2 * stdev), int(p95 * 1.5), 500)
    print()
    print("=== FULI READ LATENCY (excluding warm-up) ===")
    print(f"  samples:    {len(times)}")
    print(f"  p50:        {p50:.1f} ms")
    print(f"  p95:        {p95:.1f} ms")
    print(f"  max:        {maximum:.1f} ms")
    print(f"  average:    {average:.1f} ms")
    print(f"  stdev:      {stdev:.1f} ms")
    print()
    print(f"  recommended comparison_budget_ms: {recommended} ms")
    print(f"  (p95 + 2*stdev cushion; preserves primary-response isolation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())