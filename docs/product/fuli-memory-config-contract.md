# Fuli Memory for Hermes — Configuration and Migration Contract

## 1. Legacy to Product Migration

### 1.1 Legacy Schema (`memory.shadow`)

The legacy shadow pilot used:

```yaml
memory:
  provider: shadow
  shadow:
    enabled: true
    primary_provider: honcho
    secondary_provider: fuli
    mirror_writes: true
    compare_reads: true
    sample_rate: 0.05
    namespace: hermes:shadow-pilot
    comparison_budget_ms: 2000
    comparison_max_workers: 1
    comparison_max_queue_size: 1024
    capture_content: false
    previous_provider: honcho
    min_secondary_success_rate: 0.90
```

### 1.2 Product Schema (`memory.fuli_product`)

```yaml
memory:
  provider: shadow
  fuli_product:
    schema_version: 1
    migration_version: 1
    mode: shadow
    previous_mode: mirror
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
      queue_size: 1024
    privacy:
      capture_content: false
    rollback:
      previous_provider: honcho
      auto_rollback_on:
        - primary_mutation
        - namespace_violation
        - raw_content_leak
    quality:
      min_secondary_success_rate: 0.90
      min_adjudicated_samples: 100
      max_queue_drops: 0
```

### 1.3 Migration Rules

| Legacy key | Product key | Transform |
|---|---|---|
| `memory.shadow.enabled` | `memory.fuli_product.mode` | `true` + `compare_reads=true` + `sample_rate>0` → `shadow`; `true` + `mirror_writes` → `mirror`; otherwise `off` |
| `memory.shadow.primary_provider` | `memory.fuli_product.primary_provider` | copied |
| `memory.shadow.secondary_provider` | `memory.fuli_product.secondary_provider` | copied |
| `memory.shadow.mirror_writes` | `memory.fuli_product.mirror_writes` | copied |
| `memory.shadow.compare_reads` | `memory.fuli_product.compare_reads` | copied |
| `memory.shadow.sample_rate` | `memory.fuli_product.sample_rate` | copied |
| `memory.shadow.namespace` | `memory.fuli_product.namespace` | copied |
| `memory.shadow.comparison_budget_ms` | `memory.fuli_product.comparison.budget_ms` | copied |
| `memory.shadow.comparison_max_workers` | `memory.fuli_product.comparison.workers` | copied |
| `memory.shadow.comparison_max_queue_size` | `memory.fuli_product.comparison.queue_size` | copied |
| `memory.shadow.capture_content` | `memory.fuli_product.privacy.capture_content` | copied |
| `memory.shadow.previous_provider` | `memory.fuli_product.rollback.previous_provider` | copied |
| `memory.shadow.min_secondary_success_rate` | `memory.fuli_product.quality.min_secondary_success_rate` | copied |
| `memory.shadow.sampling_seed` | `memory.fuli_product.sampling_seed` | copied if present |

- The legacy `memory.shadow` block is preserved in the config file for audit purposes.
- The product only reads `memory.fuli_product`.
- Migration is idempotent: running it twice produces the same `fuli_product` block.
- Migration is dry-runnable: `hermes memory fuli install --dry-run` returns the target config without writing.
- A backup is always created before any config write.

## 2. Validation Rules

| Mode | Required | Forbidden |
|---|---|---|
| `off` | none | none |
| `mirror` | `mirror_writes=true` | `compare_reads=true` is ignored; `sample_rate` ignored |
| `shadow` | `compare_reads=true`, `sample_rate > 0` | `capture_content=true` |
| `canary` | `sample_rate > 0` | `canary` is not implemented in v0.1.0 |

Additional invariants:
- `primary_provider != secondary_provider`
- `schema_version == 1`
- `privacy.capture_content == false` for any active mode
- Fuli package must be importable for `mirror`, `shadow`, `canary`
- Hermes version must be in supported range

## 3. Atomicity Guarantees

- Every config mutation uses `hermes_cli.config.atomic_config_write`.
- A backup is written before mutation.
- The active `memory.provider` is only updated during `rollback` or `uninstall`.
- If the write fails, the original config remains untouched.
- The product does not edit config outside of `memory.fuli_product`, `memory.shadow`, and `memory.provider`.

## 4. Backward Compatibility

- If `memory.fuli_product` is absent, the product reads `memory.shadow` and migrates in-memory.
- If neither block exists, the product is OFF.
- Generic Hermes code that reads `memory.provider` continues to work unchanged.
- The Fuli and Shadow providers remain in their existing directories.

## 5. Rollback Contract

`hermes memory fuli rollback`:

1. Copies `config.yaml` to a timestamped backup.
2. Sets `memory.provider = rollback.previous_provider` (default `honcho`).
3. Sets `memory.fuli_product.mode = off`.
4. Sets `memory.fuli_product.previous_mode` to the mode before rollback.
5. Writes the config atomically.
6. Does not delete the comparison database.

`hermes memory fuli uninstall --preserve-data`:

1. Copies `config.yaml` to a timestamped backup.
2. If `memory.provider` is `shadow` or `fuli`, resets it to `rollback.previous_provider`.
3. Sets `memory.fuli_product.mode = off`.
4. Preserves the legacy `memory.shadow` block.
5. Does not delete data files unless `--preserve-data=false`.

## 6. Schema Evolution

- `schema_version` is the persisted config schema version.
- `migration_version` is the highest migration applied to the file.
- When `schema_version` is greater than the product's `CURRENT_SCHEMA_VERSION`, the product refuses to operate and reports `migration_required`.
- Unknown keys are preserved in `_unknown_fields` and re-serialized; they do not cause validation errors.
- New fields added in future schema versions must have sensible defaults so older configs remain loadable.
