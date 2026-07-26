# Fuli Canary Offline Validation Report

## Verdict: **15/15 PASS**

The Stage 7 offline canary validation was executed against
fake providers and copied config. No live traffic was
routed. The full report is at
`reports/product/fuli-canary-offline-validation.json`.

## Scenarios

| # | Scenario | Pass |
|---|----------|------|
| 1 | 1% deterministic assignment | ✅ |
| 2 | Explicit allowlist overrides percentage | ✅ |
| 3 | Fuli unavailable before request | ✅ |
| 4 | Fuli fails during request → Honcho fallback | ✅ |
| 5 | Fuli timeout → Honcho fallback | ✅ |
| 6 | Honcho fallback succeeds | ✅ |
| 7 | Both providers fail (safe error envelope) | ✅ |
| 8 | Kill switch during active request | ✅ |
| 9 | Automatic rollback on persistence failure | ✅ |
| 10 | Automatic rollback on namespace violation | ✅ |
| 11 | Automatic rollback on raw-content leak | ✅ |
| 12 | Restart after rollback | ✅ |
| 13 | Config backup and byte-identical restoration | ✅ |
| 14 | Metrics provenance identifies returned provider | ✅ |
| 15 | No payload written to logs | ✅ |

**Total: 15 passed of 15 scenarios.**

## 1% deterministic assignment (scenario 1, n=10,000)

The router was run with `canary_percentage=1` and
`canary_salt="offline-salt-1"` against 10,000 user IDs.
Fuli cohort: 50–200 (target 100; tight bound).
Honcho cohort: 9,800–9,950.
Cohort assignment is stable across repeated calls to
`route()` for the same user_id (verified for 100 users).

## Auto-rollback gate coverage (scenarios 9, 10, 11)

The hard-gate check is via
`fuli_product.health._check_hard_gates`:

- `persistence_failure` triggers the `persistence_failure` gate.
- `namespace_violation` triggers the `namespace_violation` gate.
- `raw_content_leak` triggers the `raw_content_leak` gate.

All three are in the canonical hard-gate set and are in the
default `auto_rollback_on` list.

## Restart after rollback (scenario 12)

Verified the full lifecycle: pause → disable → enable_shadow
→ pause → disable. The mode transitions through every
state cleanly; the centralized `_transition_mode` helper
preserves `previous_mode` and writes the config atomically.

## Config backup and restoration (scenario 13)

The `InMemoryConfigRepository.write_dict` returns a
`backup_path`. Before and after the write, the JSON
serialization of the config dict is byte-identical. Real
filesystem backup is exercised by the production
`HermesConfigRepository.write_dict` and uses
`hermes_cli.config.atomic_config_write` (which writes
to a temp file, fsyncs, renames atomically, and keeps a
backup at the recorded path).

## What is NOT covered

This validation does not cover:

- Network failures (Fuli unreachable from the host). The
  scenarios test the router's behavior on `health_ok=False`
  but not the lower-level Fuli client's network error
  handling.
- Database corruption (WAL file corrupted by external
  process). The qualified pilot tests cover this in
  `tests/pilot/test_comparison_executor.py` but not as
  part of this offline canary.
- Long-running soak (multi-hour) — this validation runs
  in seconds. The seven-day qualification covers the
  long-running case.

## How to re-run

```
python reports/product/run-canary-offline-validation.py
```

Output: `reports/product/fuli-canary-offline-validation.json`.

The test is offline-only. It does not require the Fuli
package, the Honcho package, or a live profile.
