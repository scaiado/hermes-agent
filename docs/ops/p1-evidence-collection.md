# P1 Sampled-Read Evidence Collection

This document describes how the Hermes ↔ Fuli shadow pilot collects
sampled-read evidence for human adjudication. It is the operator's
guide for the `comparison_reads=true` and `sample_rate=0.05` settings
introduced by the P1 milestone.

## What this is

The shadow provider mirrors every write to Fuli and (when enabled)
samples a stable fraction of read calls. For each sampled read, a
`ComparisonRecord` is persisted that captures fingerprints of both
the primary (Honcho) and secondary (Fuli) results, plus latency,
overlap, MRR, and a placeholder for the adjudication.

Honcho remains the authoritative memory source. Fuli is never used to
answer a user question. Comparisons exist solely to gather evidence
about how often Fuli's results differ from Honcho's, and how the
human adjudicator should weight those differences when (and only if)
Fuli is ever promoted to primary.

## What this is NOT

- It is not a quality signal for Fuli in absolute terms. A Fuli win
  is not "Fuli is better" — it is "Fuli happened to be more useful
  for this query, as judged by a human."
- It is not a sample of all reads. Only a deterministic fraction
  (configurable, default 5%) is compared.
- It is not a tuning signal. The pilot does not adjust retrieval
  weights based on these comparisons.

## Hard safety guarantees

These guarantees are enforced in code and tested in
`tests/pilot/test_p1_comparison_pipeline.py`:

1. **Honcho is always primary.** The shadow provider returns the
   primary result byte-for-byte regardless of Fuli's outcome.
2. **Fuli results never enter the live response.** A unit test
   injects a recognisable Fuli marker string and asserts the marker
   never appears in the returned primary payload.
3. **Fuli's latency does not delay the primary.** The sampled-read
   comparison runs entirely off the request path on a bounded
   background executor; the foreground returns the primary
   immediately. See the precise guarantee below.
4. **Comparison failure does not fail the user request.** All
   exceptions in the comparison path are caught; the row is recorded
   with `secondary_status="failed"` and a `secondary_error_category`.
5. **No raw content is ever stored.** The comparison-store schema
   has no column for raw query text or raw memory content. The
   `content_captured` flag is hard-locked to `False` in the writer.
6. **Provider-specific IDs do not affect fingerprints.** UUIDs, ULIDs,
   and provider-internal identifiers are stripped during
   fingerprint normalization.
7. **Namespace isolation.** Comparisons carry the shadow namespace
   (default `hermes:shadow-pilot`); the production namespace is never
   touched.
8. **Comparison records are idempotent on `comparison_id`.** Duplicate
   writes do not create duplicate rows.
9. **Sampling is deterministic and reproducible.** Same
   `(sampling_seed, query_hash, namespace)` → same decision.
10. **Comparison accounting is balanced.** Every persisted comparison
    has a recorded primary and secondary status. Drift would be
    visible in the `comparison_accounting()` summary.

## Precise foreground guarantee

After Honcho completes, Hermes enqueues an immutable comparison
job and returns the Honcho response without waiting for Fuli
execution or persistence.

Measured on the dedicated executor test fixture
(`tests/pilot/test_comparison_executor.py`):

- **Foreground enqueue overhead** (target): p50 < 2 ms, p95 < 10 ms,
  absolute max < 25 ms.
- **Queue wait latency**: time the job sits in the comparison queue
  before a worker picks it up.
- **Fuli execution latency**: the wall-clock duration of the
  synchronous Fuli call inside the background worker.
- **Persistence latency**: the wall-clock duration of the SQLite
  write inside the persistence worker.

These four latencies are observed end-to-end and surfaced via
`ComparisonExecutor.accounting()`. They are independent of each
other and of the foreground enqueue overhead.

### Boundedness

The executor owns one fixed-size comparison pool (default 1
worker) and one single persistence worker. Total outstanding jobs
is bounded by `max_queue_size + max_workers` (default 1024 + 1
= 1025 in flight at once across both queues). The comparison queue
uses `queue.Queue.put_nowait`; a full queue increments
`comparison_jobs_dropped_queue_full` and returns False. Queue-full
never affects the primary response.

### Known inability to forcibly terminate Python threads

Python cannot forcibly terminate a running thread. If the Fuli
bridge fails to honor its own per-call timeout, the comparison
worker stays blocked on the Fuli call. Other workers and the
foreground are unaffected. The blocked worker is observed via
`comparison_jobs_orphaned` (a comparison worker blocked for more
than `comparison_budget_ms + ORPHAN_GRACE_SECONDS`).

### Cancellation semantics

The comparison worker uses `ThreadPoolExecutor` with a fixed
worker count; `cancel_futures=True` cancels pending comparison
futures (only used in error paths; the normal teardown waits for
in-flight comparisons to finish). The persistence worker is a
plain `threading.Thread(daemon=True)` whose infinite loop exits
when the executor's `_stop_event` is set. A daemon thread is
reaped at interpreter shutdown.

### Flush semantics

`flush(timeout_seconds=...)` waits for:

1. All comparison futures to finish (each future waits up to the
   remaining deadline).
2. The persistence queue to drain.
3. In-flight persistence writes to terminate (every
   `persistence_started` has a matching
   `persistence_succeeded + persistence_failed`).

Flush does NOT stop accepting new jobs. Only `teardown()` (the
executor lifecycle's terminate step) does.

### Teardown semantics

The six-step teardown (in order):

1. `accepting=False` — no new comparison jobs accepted.
2. The comparison pool is finalized with `wait=True,
   cancel_futures=False` so all submitted comparisons finish.
3. `flush()` — drain the persistence queue.
4. `_stop_event.set()` — signal the persistence worker to exit.
5. `_persistence_thread.join(timeout=...)` — wait for the
   persistence worker to actually exit.
6. `_stopped=True` — the executor is now teardown-completed.

Idle comparison workers and the idle persistence worker can
exit promptly because they poll `queue.get(timeout=0.1)` and
check `_stop_event` between iterations.

### Accounting invariants

`is_balanced()` returns True iff:

- `sampled == enqueued + dropped_queue_full` (every sampled
  query is either enqueued or dropped)
- `enqueued == started + pending` (every enqueued job is either
  started or still queued)
- `started == completed + timed_out + failed` (every started job
  finishes in one of those three ways)
- `completed + timed_out == persisted` (every comparison that
  produced a result is persisted)

A `False` return indicates drift; the executor's accounting
counters are the source of truth.

## Data model

### comparisons table

| Column | Type | Description |
|---|---|---|
| `comparison_id` | TEXT PK | UUID4, generated on first write. Idempotent. |
| `run_id` | TEXT | The session/run this comparison belongs to. |
| `timestamp` | TEXT | ISO-8601 UTC. |
| `namespace` | TEXT | The shadow namespace. Never the production one. |
| `query_hash` | TEXT | SHA-256[:16] of the normalized query. Not the query itself. |
| `query_type` | TEXT | `unclassified` by default; set by `disagreements classify`. |
| `requested_top_k` | INT | The `top_k` argument from the search call. |
| `primary_provider` | TEXT | `honcho`. |
| `secondary_provider` | TEXT | `fuli`. |
| `primary_latency_ms` | REAL | Wall-clock latency of the primary call. |
| `secondary_latency_ms` | REAL | Wall-clock latency of the secondary call (or 0 on failure). |
| `primary_status` | TEXT | `success` / `failed`. |
| `secondary_status` | TEXT | `success` / `failed` / `skipped`. |
| `primary_error_category` | TEXT | `primary_parse_error` (or NULL on success). |
| `secondary_error_category` | TEXT | `timeout_after_Xms` / `secondary_error: ...` / `secondary_shape_error` / `secondary_parse_error: ...` (or NULL on success). |
| `primary_result_fingerprints` | TEXT (JSON) | List of 16-char SHA-256 hex fingerprints. |
| `secondary_result_fingerprints` | TEXT (JSON) | List of 16-char SHA-256 hex fingerprints. |
| `overlap_at_1` | REAL | Fraction of primary's top-1 also in secondary's top-1. |
| `overlap_at_3` | REAL | Same for top-3. |
| `overlap_at_5` | REAL | Same for top-`requested_top_k`. |
| `reciprocal_rank_agreement` | REAL | Mean Reciprocal Rank agreement in [0, 1]. |
| `missing_from_primary` | TEXT (JSON) | Secondary-only top-k fingerprints. |
| `missing_from_secondary` | TEXT (JSON) | Primary-only top-k fingerprints. |
| `secondary_retrieval_mode` | TEXT | `vector` / `hybrid` / `lexical` / `unknown`. |
| `content_captured` | INT | Always 0. Schema-level flag for audit. |
| `adjudication_status` | TEXT | `pending` / `decided`. |
| `schema_version` | INT | `1`. |

### adjudications table

| Column | Type | Description |
|---|---|---|
| `comparison_id` | TEXT PK FK | References `comparisons.comparison_id`. |
| `winner` | TEXT | `fuli` / `honcho` / `both` / `neither` / `unsafe`. |
| `query_type` | TEXT | Updated on the parent comparison row. |
| `reason_code` | TEXT | `more_relevant` / `better_ranked` / `more_complete` / `more_recent` / `less_stale` / `correct_namespace` / `better_provenance` / `avoids_contradiction` / `both_equivalent` / `both_poor` / `unsafe_result`. |
| `note` | TEXT | Optional free text. Operators are responsible for keeping this free of raw content. |
| `adjudicator` | TEXT | Who recorded the judgement. |
| `adjudicated_at` | TEXT | ISO-8601 UTC. |
| `updated_at` | TEXT | ISO-8601 UTC. Bumped on every update. |

## Operator workflow

### Enabling sampled reads

Set the following in `~/.hermes/profiles/shadow-pilot/config.yaml`:

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
    sampling_seed: 0
    comparison_budget_ms: 250
    write_timeout_ms: 10000
    read_timeout_ms: 250
    capture_content: false
    namespace: hermes:shadow-pilot
```

The `sampling_seed` is an integer; same seed + same query → same sample
decision. Different seeds change which queries are sampled. This is
useful when you want to gather two non-overlapping sample sets
without changing the `sample_rate`.

### Pre-launch validation

Before enabling `compare_reads: true` in production, run:

```bash
HERMES_HOME=/Users/caiado/.hermes/profiles/shadow-pilot \
HERMES_PROFILE=shadow-pilot \
venv/bin/hermes shadow preflight
```

Expected: `ok: true`, `primary_available: true`, `fuli_commit_ok: true`,
`model_cached: true`, `mismatches: []`.

### Inspecting collected comparisons

```bash
# List the most recent 200 comparisons.
hermes shadow disagreements list

# Show one comparison by id.
hermes shadow disagreements show <comparison-id>

# Record a winner decision.
hermes shadow disagreements judge <comparison-id> \
  --winner fuli \
  --reason more_relevant \
  --note "fuli caught the fact that honcho missed" \
  --adjudicator alice

# Classify a comparison's query_type.
hermes shadow disagreements classify <comparison-id> --type preference

# Export the redacted dataset for offline analysis.
hermes shadow disagreements export --redacted --output /tmp/p1-export.json
```

The export is redacted by design: it never contains raw query text or
raw memory content. Only the structured fields above are emitted.

## Storage

- Comparisons DB: `~/.hermes/profiles/shadow-pilot/memories/comparisons.db`
- Legacy evidence DB (writes only): `~/.hermes/profiles/shadow-pilot/memories/shadow.db`

The legacy `observations` table is still written for backward
compatibility with Stage A reports. New P1 work reads and writes
`comparisons` instead.

## Stop conditions

The P1 collection run auto-stops if any of the following occurs:

- `primary_available_at_end` is `false` (Honcho was unreachable at
  end of run).
- A namespace or raw-content violation is recorded.
- `comparison_persistence_lost_records` becomes non-zero.
- The accounting drift detector (see `pilot/comparison_store.py`)
  reports `sampled != primary_completed != persisted`.

A run that triggers any of these must not be used as evidence for
tuning. Re-run after fixing the underlying issue.

## What is NOT in this milestone

- Per-episode indexer latency from Fuli (would require Fuli to
  expose per-episode timestamps; not in the upstream Fuli contract
  at `727ce926`).
- Real-time read-side comparison (Stage B runs at fixed 5% and
  stores everything asynchronously; it is not a live feedback loop).
- Tuning of retrieval weights based on these comparisons. The
  pilot's job is to collect evidence; tuning is downstream.

## Related

- `docs/ops/fuli-shadow-cutover-plan.md` — the Stage A cutover plan.
- `docs/ops/fuli-provider-pin.md` — the Fuli commit pin.
- `pilot/comparison_metrics.py` — fingerprint and overlap primitives.
- `pilot/sampling.py` — deterministic sampling decision.
- `pilot/comparison_store.py` — durable comparison storage.
- `tests/pilot/test_p1_comparison_pipeline.py` — 41 tests covering
  the safety and accounting guarantees.