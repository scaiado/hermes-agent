# Fuli Memory for Hermes — Product Specification

## 1. User Promise

> **Add a private, local-first, observable memory engine to Hermes while preserving the authoritative response path and providing a reversible migration from Honcho.**

Fuli Memory for Hermes lets users keep Honcho as the trusted source of truth, mirrors new memory writes to a local Fuli index, and optionally compares Honcho and Fuli read results without ever exposing Fuli results to the live model output. It is a stage-gated product: OFF → MIRROR → SHADOW → CANARY → PRIMARY. PRIMARY mode is out of scope until representative quality gates pass and explicit user approval is recorded.

## 2. Product Modes

| Mode | Honcho authoritative | Fuli initialized | Mirrors writes | Compares reads | Fuli in live output | Exit criteria |
|---|---|---|---|---|---|---|
| **OFF** | yes | no | no | no | no | default state |
| **MIRROR** | yes | yes | yes | no | no | safe baseline; proves write path |
| **SHADOW** | yes | yes | yes | sampled | no | safe evaluation; live comparison evidence |
| **CANARY** | yes (fallback) | yes | yes | sampled | limited, approved queries | explicit rollback plan; user approval |
| **PRIMARY** | no | yes | yes | no | yes | **not implemented until quality gates pass** |

Mode transitions are allowed only in the upward direction (OFF→MIRROR→SHADOW→CANARY) or to **PAUSED**. A transition to PAUSED stops all new comparisons and mirrors but leaves existing data intact. From PAUSED, the user can revert to the previous mode or to OFF. A transition to OFF/rollback restores Honcho-only operation.

## 3. Status Model

Every `hermes memory fuli status` call returns a deterministic status:

| Status | Meaning | Auto-recoverable | User action |
|---|---|---|---|
| `not_installed` | Fuli provider plugin is not present or unavailable. | maybe | `install` |
| `installed` | Fuli is installed but not configured as a memory provider. | yes | `enable` |
| `unavailable` | Fuli package import failed or dependencies missing. | no | install deps / check logs |
| `healthy` | Mode is active, primary is reachable, comparison accounting is balanced, no hard gates triggered. | — | none |
| `degraded` | Comparison success rate is below healthy threshold but above critical, or resource pressure is high. | maybe | `pause` or `doctor` |
| `paused` | Operator explicitly paused the product. | yes | `enable` or `rollback` |
| `auto_paused` | A hard gate fired; no new comparisons or mirrors are running. | no | `doctor`, fix root cause, then `enable` |
| `migration_required` | Config schema version is older than required. | yes | `enable --dry-run` then `enable` |
| `incompatible` | Hermes version or Fuli version is outside the supported matrix. | no | upgrade/downgrade |
| `rollback_required` | Primary output mutation or namespace violation detected. | no | `rollback` immediately |

Status is computed on demand, not inferred from process existence alone.

## 4. Configuration Schema

```yaml
memory:
  provider: shadow          # or honcho, fuli, etc.
  fuli_product:
    schema_version: 1
    mode: shadow            # off | mirror | shadow | canary | paused
    previous_mode: ""        # used after pause / rollback
    primary_provider: honcho
    secondary_provider: fuli
    namespace: hermes:shadow-pilot
    mirror_writes: true
    compare_reads: true
    sample_rate: 0.05
    sampling_seed: 0
    comparison:
      budget_ms: 2000
      workers: 1
      queue_size: 128
    privacy:
      capture_content: false
    rollback:
      previous_provider: honcho
      auto_rollback_on: [primary_mutation, namespace_violation, raw_content_leak, persistence_failure]
    quality:
      min_secondary_success_rate: 0.90
      min_adjudicated_samples: 100
      max_queue_drops: 0
```

All product-specific settings live under `memory.fuli_product`. The legacy `memory.fuli` and `memory.shadow` blocks are supported for migration but are not the canonical source after product install.

## 5. CLI Contract

### Commands

```bash
hermes memory fuli install [--mode off|mirror|shadow]
hermes memory fuli doctor
hermes memory fuli status [--json]
hermes memory fuli enable --mode mirror [--dry-run]
hermes memory fuli enable --mode shadow --sample-rate 0.05 [--dry-run]
hermes memory fuli pause [--reason "operator request"]
hermes memory fuli report [--period 1h|24h|7d] [--json]
hermes memory fuli export --redacted [--run-id RUN_ID] [--output PATH]
hermes memory fuli rollback [--to honcho]
hermes memory fuli uninstall [--preserve-data]
```

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | success / healthy |
| `1` | generic failure |
| `2` | invalid arguments / config |
| `3` | Fuli unavailable / incompatible |
| `4` | degraded / auto_paused |
| `5` | rollback required |
| `10` | migration required |
| `99` | dry-run would fail |

### JSON Schema (`status --json`)

```json
{
  "schema_version": 1,
  "status": "healthy",
  "mode": "shadow",
  "hermes_version": "0.19.0",
  "fuli_version": "0.x.x",
  "fuli_commit": "727ce926...",
  "primary_provider": "honcho",
  "secondary_provider": "fuli",
  "namespace": "hermes:shadow-pilot",
  "privacy": {
    "capture_content": false,
    "raw_content_rows": 0,
    "namespace_violations": 0
  },
  "health": {
    "primary_available": true,
    "secondary_available": true,
    "last_check": "2026-07-20T12:00:00Z"
  },
  "accounting": {
    "sampled": 4483,
    "enqueued": 4483,
    "started": 4483,
    "completed": 4481,
    "timed_out": 2,
    "failed": 0,
    "dropped_queue_full": 0,
    "persisted": 4483,
    "pending": 0,
    "orphaned": 0,
    "persistence_failed": 0,
    "unexpected_collision": 0,
    "primary_mutations": 0,
    "queue_depth": 0,
    "active_jobs": 0,
    "outstanding_jobs": 0,
    "admission_capacity": 129,
    "admission_available": 129,
    "is_balanced": true
  },
  "last_checkpoint": "2026-07-20T11:42:15Z"
}
```

## 6. Hard Gates (Auto-Pause)

The product automatically pauses (status = `auto_paused`) when any of the following occur:

1. `primary_mutations > 0` — primary output changed between direct and shadowed calls.
2. `namespace_violations > 0` — a comparison row was written outside `memory.fuli_product.namespace`.
3. `raw_content_leak > 0` — a persisted row has `content_captured = true` or a raw query/memory text appears in logs/DB.
4. `persistence_failed > 0` — any SQLite write failure.
5. `dropped_queue_full > 0` — admission capacity was exhausted.
6. `orphaned > 0` — a comparison worker blocked longer than the orphan threshold.
7. Secondary success rate below `min_secondary_success_rate` over at least 20 samples.
8. SQLite integrity check fails.
9. Fuli version is outside the supported range.
10. Config schema version is newer than the product understands.

Auto-pause is cooperative: the provider stops accepting new comparison/mirror work but keeps the primary path intact and does not restart the process.

## 7. Privacy Model

- By default, **no raw query text and no raw memory content** is persisted.
- Only fingerprints (SHA-256[:16] of canonical strings) are stored in the comparison database.
- `content_captured` is a boolean audit column; it must remain `false` in normal operation.
- The `secondary_search_args` dict carries the raw query only in-memory and is never logged or persisted.
- The `fuli export --redacted` command removes any in-memory text that may exist in diagnostic fields before writing output.
- Provider names (`honcho`, `fuli`) and namespace names are allowed to appear as schema values; they are not secrets.

## 8. Quality Evaluation Gate

Before CANARY mode is offered, the operator must complete a representative evaluation:

- at least 200 representative comparisons
- at least 100 adjudicated comparisons
- at least 10 adjudications per major query type (profile, preference, project, episodic, exact, semantic, recent, contradiction)
- no privacy violations
- Fuli non-inferior overall (no statistically significant loss in win rate)
- no critical query-type regression

Adjudication is blind: the adjudicator sees two result sets labeled A and B, plus the query type, without knowing which provider produced which. Rationale is captured via closed reason codes, not free text, to avoid raw-content leaks.

## 9. Rollback

`hermes memory fuli rollback` performs:

1. Set `memory.provider = previous_provider` (default `honcho`).
2. Set `memory.fuli_product.mode = off`.
3. Stop the shadow executor and Fuli provider cleanly (bounded shutdown).
4. Remove Fuli from `memory.provider` if it was the active provider.
5. Write an atomic config backup before mutation and record the action in the audit log.
6. Do **not** delete comparison data unless `--purge-data` is passed.

Rollback is atomic from the user's perspective: either the config is restored or an error is returned. The comparison database is preserved for forensic analysis.

## 10. Out of Scope

- PRIMARY mode until representative quality evaluation passes and explicit user approval is recorded.
- Fuli retrieval weight tuning in this release.
- Exposing Fuli results to the live model answer in SHADOW mode.
- Real-time dashboard integration for the product status (CLI-only for v0.1.0).
- Mobile-specific UI flows.
- Automatic promotion from SHADOW to CANARY.

## 11. Success Criteria for v0.1.0

- [ ] `install`, `doctor`, `status`, `enable`, `pause`, `report`, `export`, `rollback`, `uninstall` commands work end-to-end.
- [ ] Config migration from legacy `memory.shadow` to `memory.fuli_product` is idempotent and dry-runnable.
- [ ] Status JSON schema is stable and versioned.
- [ ] All hard gates trigger auto-pause deterministically.
- [ ] Primary output remains invariant in SHADOW mode.
- [ ] No raw content leaks in default configuration.
- [ ] Rollback restores Honcho-only operation in ≤ 5 seconds.
- [ ] Hermes boots and runs normally when Fuli is not installed.
- [ ] Hermes boots and runs normally when Fuli is installed but unavailable.
- [ ] Compatibility matrix (Phase 7) passes for the current and pending Hermes versions.
- [ ] 168-hour controlled real-provider shadow soak is completed and tagged.
