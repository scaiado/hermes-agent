# Fuli Memory for Hermes — Observability Design

## 1. Telemetry Requirements

Every shadow mode comparison produces a deterministic set of telemetry:

| Metric | Source | Type | Persisted | Used by |
|---|---|---|---|---|
| `sampled` | executor admission | counter | store | status, report, health |
| `enqueued` | executor admission | counter | memory | status |
| `started` | executor worker | counter | store | status, health |
| `completed` | executor worker | counter | store | status, health |
| `timed_out` | executor worker | counter | store | status, health |
| `failed` | executor worker | counter | store | status, health |
| `dropped_queue_full` | executor admission | counter | store | hard gate |
| `persisted` | store write callback | counter | store | accounting invariant |
| `pending` | executor state | gauge | memory | status |
| `orphaned` | executor state | gauge | memory | hard gate |
| `persistence_failed` | store exception | counter | store | hard gate |
| `unexpected_collision` | unique ID collision | counter | store | hard gate |
| `primary_mutations` | result fingerprint comparison | counter | store | hard gate |
| `queue_depth` | executor lock | gauge | memory | status |
| `active_jobs` | executor lock | gauge | memory | status |
| `outstanding_jobs` | executor lock | gauge | memory | status |
| `admission_capacity` | config | constant | config | status |
| `admission_available` | executor lock | gauge | memory | status |
| `is_balanced` | executor accounting | boolean | memory | health |

## 2. Accounting Invariant

The product maintains the invariant:

```
sampled == completed + timed_out + failed + dropped_queue_full + active + queued
persisted == completed + timed_out + failed - persistence_failed
```

Any deviation triggers `auto_paused` with gate `accounting_invariant_broken`.

## 3. Status JSON Contract

The `hermes memory fuli status --json` output is the canonical observability
artifact. It is versioned by `schema_version` and is stable across the v0.1.x
release series. See `fuli-memory-product-spec.md` §5 for the full schema.

## 4. Logging Rules

- No raw query text or memory content is logged at INFO or higher.
- Fingerprinted identifiers (first 16 chars of SHA-256) are logged at DEBUG.
- Provider names and namespaces are logged at INFO.
- Comparison errors are logged at ERROR with the comparison_id and run_id only.
- Config mutations are logged at INFO with action and backup path.

## 5. Export and Diagnostics

`hermes memory fuli export --redacted` produces a JSON bundle containing:

- `status` (redacted)
- `report` (aggregated by query type, no raw content)
- `schema_version` of the bundle

The export never includes:
- raw query strings
- raw memory results
- `content_captured` rows
- user environment variables
- API keys

## 6. Health Checks

| Check | Frequency | Trigger | Auto-action |
|---|---|---|---|
| Fuli importable | on `status`/`doctor` | failure | `unavailable` status |
| Hermes version in range | on `status`/`doctor` | failure | `incompatible` status |
| SQLite integrity | on `status` | failure | `auto_paused` (sqlite_integrity_failure) |
| Hard gates | on every `status` | any gate | `auto_paused` |
| Secondary success rate | on `status` | below threshold | `degraded` / `auto_paused` |
| Queue drops | on `status` | any drop | `auto_paused` |
| Admission balance | on `status` | unbalanced | `auto_paused` |

## 7. Prometheus-Ready Labels

Although the v0.1.0 product exposes metrics only via CLI, the accounting keys
are named so they can be emitted as Prometheus-style counters/gauges in a
future release:

```
fuli_product_comparisons_total{status="completed",query_type="profile"}
fuli_product_comparisons_total{status="timed_out",query_type="profile"}
fuli_product_hard_gates_total{gate="primary_mutations"}
fuli_product_status{mode="shadow",status="healthy"}
```

## 8. Alerting Policy (Recommended)

- `status` not `healthy` for more than 5 minutes → page operator.
- `auto_paused` → immediate investigation; do not auto-resume.
- `primary_mutations > 0` → stop and rollback.
- `raw_content_leak > 0` → stop, purge affected rows, investigate.
- `dropped_queue_full > 0` → increase `queue_size` or reduce `sample_rate`.

## 9. Forensic Data Model

The comparison store schema supports post-incident queries:

- `run_id` groups comparisons by user request.
- `query_type` enables stratified analysis.
- `primary_result_fingerprint` vs `secondary_result_fingerprint` detects divergence.
- `primary_mutation` detects primary output instability.
- `namespace` isolates product data from other uses of the comparison store.
- Timestamps are monotonic (`started_at`, `completed_at`) for latency analysis.

## 10. No Dashboard in v0.1.0

The first release is CLI-only. All observability data is available through:

- `hermes memory fuli status [--json]`
- `hermes memory fuli report [--period 24h] [--json]`
- `hermes memory fuli export --redacted [--output PATH]`
- `hermes memory fuli doctor [--json]`
- Direct SQLite read of `~/.hermes/fuli_product/comparisons.db` (metadata only)
