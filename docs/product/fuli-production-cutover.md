# Fuli Memory Production Cutover Contract

This document defines the exact production cutover path for replacing
Honcho with Fuli Memory through adjudicated shadow and bounded
canary stages. **It is a contract, not a deployment schedule.** No
stage advances automatically.

## Goals

- Replace Honcho with Fuli without losing user trust or
  retrievability.
- Preserve Honcho as the writable-of-record until sustained
  production evidence.
- Allow immediate global rollback at every stage.
- Avoid direct OFF → FULI_PRIMARY transitions; every step
  produces evidence that gates the next.

## Non-goals

- This contract does not authorize a live canary. A separate
  explicitly approved canary mission is required.
- This contract does not modify the running soak, the active
  profiles, or install the pending Hermes update.

## Mode definitions

### OFF
- Honcho is primary.
- No Fuli initialization.
- No mirroring.
- No comparison.
- `mode="off"`, `compare_reads=false`, `mirror_writes=false`.
- Storage: Honcho only. Fuli cold.

### MIRROR
- Honcho primary, synchronous path unchanged.
- Writes mirrored to Fuli.
- No read comparisons.
- `mode="mirror"`, `mirror_writes=true`, `compare_reads=false`.
- Storage: Honcho authoritative; Fuli mirror (no fallback).
- Failure model: Fuli mirror failure is logged but does not affect
  the live answer. (A persistent Fuli failure triggers an alert
  but not auto-pause.)

### SHADOW
- Honcho primary, synchronous path unchanged.
- Fuli reads sampled asynchronously.
- Fuli never affects the returned output.
- `mode="shadow"`, `compare_reads=true`, `mirror_writes=false`,
  `sample_rate>0`.
- Storage: Honcho authoritative. Fuli shadow only.
- Privacy: only fingerprints and the minimum required query
  arguments are persisted; raw primary response text is dropped
  at enqueue time.

### ADJUDICATED_SHADOW
- Same runtime behavior as SHADOW.
- Representative approved queries are collected.
- Blind adjudication workflow is enabled.
- Fuli output never enters live answers.
- `mode="adjudicated_shadow"` (a refinement of shadow with the
  adjudication pipeline active; the underlying sample/compare
  executor is unchanged).
- Adjudication outcomes: `fuli_better`, `honcho_better`, `tie`,
  `both_bad`, `insufficient_evidence`.
- Each adjudication is recorded with: query hash, query type,
  provider result fingerprints, latency, freshness metadata,
  retrieval mode, blind candidate ordering, outcome, and a
  rationale code.

### CANARY
- Fuli primary for an approved percentage/cohort.
- Honcho is queried synchronously (in parallel) and is the
  fallback path on any Fuli failure.
- Honcho remains writable and recoverable.
- Automatic rollback on hard gates (see below).
- `mode="canary"`, with `canary_percentage` ∈ {1, 5, 20} and
  `canary_cohort` (allowlist or hash-based deterministic).
- Sticky cohort membership: a profile's cohort does not change
  while the canary is running.
- No live answer is produced without the primary-output
  provenance being recorded.

### FULI_PRIMARY_WITH_FALLBACK
- Fuli authoritative.
- Honcho receives mirror writes.
- Honcho can be restored as authoritative immediately.
- `mode="fuli_primary_with_fallback"`, `mirror_writes=true`,
  `fallback_provider="honcho"`.
- Rollback: change `mode` back to `canary` or `shadow`; Honcho
  state is the last-known-good.

### FULI_PRIMARY (future state)
- Fuli authoritative.
- Honcho disabled only after sustained production evidence
  proves the canary and primary-with-fallback stages.
- `mode="fuli_primary"`, `mirror_writes=false`,
  `fallback_provider=null`.
- This stage is NOT entered by the cutover contract; it is
  reached only after a separate explicit, sustained-evidence
  mission.

## Forbidden direct transition

The product MUST NOT accept a direct transition from
`off` → `fuli_primary` (or any non-adjacent transition). The
lifecycle's transition helper enforces sequential progression.

## Required transitions

| from | to | required evidence |
|------|----|-------------------|
| off | mirror | install + dry-run success; Fuli provider available |
| mirror | shadow | mirror success rate >= 99% over 24h |
| shadow | adjudicated_shadow | representative corpus collected (>= 200 queries) |
| adjudicated_shadow | canary (1%) | >= 100 blind adjudications, Fuli non-inferior overall, no critical query-type regression |
| canary 1% | canary 5% | 1% canary success rate >= 99%, p95 latency within agreed threshold, no hard gates fired for 7d |
| canary 5% | canary 20% | 5% canary success rate >= 99%, p95 within threshold, no hard gates fired for 7d, >= 500 adjudications at this stage |
| canary 20% | fuli_primary_with_fallback | 20% canary success rate >= 99.5%, p95 within threshold, no hard gates fired for 14d, Fuli win rate >= agreed floor (default 0.45) |
| fuli_primary_with_fallback | fuli_primary | (separate explicit mission; not part of this contract) |

## Rollback

Every transition must support rollback to the prior safe mode.
- From canary: rollback to adjudicated_shadow.
- From adjudicated_shadow: rollback to shadow.
- From shadow: rollback to mirror or off.
- From mirror: rollback to off.
- From fuli_primary_with_fallback: rollback to canary (most
  recent successful percentage).

Rollback restores the prior `mode` and `previous_mode` recorded by
the centralized `_transition_mode` helper. Each rollback writes
a backup of the prior config; the backup path is returned in the
summary.

## Hard gates

Hard gates (per `fuli_product/health.py`) trigger automatic pause
or rollback. The set of gates and their canonical keys are:

- `primary_mutation`
- `namespace_violation`
- `raw_content_leak`
- `persistence_failure`
- `queue_drop`
- `orphaned_worker`
- `unexpected_collision`
- `sqlite_integrity_failure`
- `secondary_success_below_threshold`

`fuli_product.config.normalize_accounting` and
`normalize_hard_gates` fold legacy keys (`primary_mutations`,
`namespace_violations`, `persistence_failed`, `queue_drops`,
`dropped_queue_full`, `orphaned`, `secondary_success_rate_low`)
into the canonical set so reports never carry duplicate spellings.

## Can-stage-advance rule

No stage advances automatically. Every transition is an explicit
operator action. The transition helper refuses non-adjacent
transitions.

## Documentation hooks

- `fuli_product/lifecycle.py::_transition_mode` enforces the
  helper invariants.
- `fuli_product/health.py::_check_hard_gates` returns the canonical
  gate set.
- `fuli_product/reporting.py::Reporter.status` returns the stable
  status schema (schema_version 1) used by the cutover dashboard.
- `fuli_product/evaluation/` is the Phase 6 surface for the
  adjudicated-shadow corpus and adjudication pipeline.
