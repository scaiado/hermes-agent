# Honcho vs Fuli Superiority Scorecard

P0 reliability pass — Hermes ↔ Fuli shadow pilot.  
Last updated: 2026-07-11

## Purpose

Compare Hermes' primary memory backend (Honcho) against the local Fuli shadow
mirror across dimensions that matter for production memory: availability,
latency, privacy, observability, correctness, and operational control.

The goal is not to declare a winner in every row, but to identify where Honcho
remains superior today and where Fuli is already good enough to take over or
serve as a fallback.

## Summary

| Criterion | Honcho | Fuli | Winner | Notes |
|---|---|---|---|---|
| Data ownership | SaaS; data leaves device | Local SQLite + vectors on device | **Fuli** | Fuli keeps everything under `HERMES_HOME/memories/`. |
| Offline availability | Requires network + API key | Fully local after first init | **Fuli** | Honcho fails closed when offline; Fuli continues. |
| Latency (p50 write) | ~70 ms | ~24 ms | **Fuli** | Measured during shadow-pilot smoke; Fuli includes vector indexing. |
| Latency tail (p95) | ~2.4 s | ~2.7 s (first-call model load) | **Honcho** | Honcho's tail is network/server; Fuli's is local model load. |
| Structured status | Accepted/Indexed/Pending/Failed | Accepted/Indexed/Pending/Failed (after P0) | **Tie** | Both can now return explicit ingestion status. |
| Correlation/tracing | Session/event IDs only | Per-memory `correlation_id` + event ID | **Fuli** | P0 adds first-class correlation in Fuli. |
| Startup reconciliation | Managed by Honcho service | Implemented in Fuli (after P0) | **Tie** | Fuli now reconciles pending/failed/stale on start. |
| Recovery from delayed indexing | Honcho handles internally | Implemented: retry → indexed, error cleared, novelty backfilled | **Tie** | P0 delayed-indexing recovery passes. |
| Operational diagnostics | Limited / external | WAL, integrity, checkpoint, page stats | **Fuli** | Fuli exposes local SQLite health. |
| Multi-device sync | Native (cloud) | None; single-device | **Honcho** | Fuli has no sync today. |
| User/profile model | Rich (users, sessions, peers, conclusions) | Memories only; no user graph | **Honcho** | Fuli stores facts; Honcho models relationships. |
| Maturity / surface area | Production API, many integrations | Young, narrower API | **Honcho** | Fuli API is ingestion-focused today. |
| Cost at scale | Per-request / subscription | One-time local compute | **Fuli** | No network egress or API costs. |

## Scoring

Each row is scored **+1** for the winner, **0** for a tie, **-1** for the loser.
Honcho wins where a richer service model is the differentiator; Fuli wins where
local-first operation is the differentiator.

| Dimension | Score Honcho | Score Fuli |
|---|---|---|
| Data ownership | 0 | +1 |
| Offline availability | 0 | +1 |
| Latency p50 | 0 | +1 |
| Latency tail | +1 | 0 |
| Structured status | 0 | 0 |
| Correlation/tracing | 0 | +1 |
| Startup reconciliation | 0 | 0 |
| Recovery from delayed indexing | 0 | 0 |
| Operational diagnostics | 0 | +1 |
| Multi-device sync | +1 | 0 |
| User/profile model | +1 | 0 |
| Maturity / surface area | +1 | 0 |
| Cost at scale | 0 | +1 |
| **Total** | **+4** | **+6** |

## Interpretation

- **Fuli is operationally superior for local, write-heavy, privacy-sensitive
  memory.** Lower latency, no network dependency, full data ownership, and richer
  local diagnostics make it a strong candidate for primary memory on a single
  device.
- **Honcho remains superior where a managed user graph, multi-device sync, and
  broader ecosystem integrations matter.** Its profile/peer/conclusion model is
  not replicated in Fuli yet.
- **P0 changes the reliability equation.** Before P0, Fuli lacked explicit
  ingestion status, correlation IDs, and delayed-indexing recovery. After P0,
  Fuli can report whether every write was accepted, indexed, pending, or failed,
  and can recover from transient failures on startup.

## Go/no-go for shadow cutover

| Condition | Status | Evidence |
|---|---|---|
| P0 strict xfails converted to passing | In progress | Fuli subagent running; must finish. |
| 15-minute real validation | Blocked | Wait for Fuli P0 completion and drain fix. |
| Primary classification correct | Fixed | `primary_classifier.py` now recognizes `result` field. |
| Mirror only on primary success | Fixed | Shadow provider uses `primary_success` flag. |
| Zero unexplained attempts | Not yet verified | Needs real validation. |
| All tests green | In progress | Hermes plugin/pilot tests pass; Fuli tests pending. |

## Recommendation

Keep Honcho as the production primary until:

1. Fuli P0 strict xfails are passing and the branch is pushed.
2. The 15-minute real shadow pilot shows ≥99% accepted writes and ≥99% eventual
   indexing after drain.
3. A four-hour soak shows no memory leaks, no unexplained attempts, and no
   namespace/privacy violations.

After that, Fuli can be promoted to primary for local sessions, with Honcho as
an optional sync target.

## Sources

- `/Users/caiado/.hermes/hermes-agent/pilot/primary_classifier.py`
- `/Users/caiado/.hermes/hermes-agent/plugins/memory/shadow/__init__.py`
- `/Users/caiado/.hermes/hermes-agent/pilot/shadow_pilot.py`
- `/Users/caiado/.hermes/hermes-agent/plans/p0-shadow-reliability.md`
- Shadow-pilot smoke test logs, 2026-07-11
