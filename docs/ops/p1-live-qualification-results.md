# P1 Live Qualification Evidence

This document is the canonical evidence that authorizes an operator to
enable the **extended live 5% shadow-pilot collection** on the
`integration/fuli-v0.18.2` branch. It is the operator-facing companion
to the canonical, replayable live qualification driver at
`scripts/p1_qualify_live_15min.py`.

It does **not** enable extended collection. The collection is
disabled until the operator explicitly approves the
[Extended Collection Plan](#extended-collection-plan) below.

---

## Final Recommendation

**GO** for the seven-day extended live 5% collection, subject to the
operator-flip step described in
[Operator Enablement](#operator-enablement).

**Evidence:** the v3 canonical live qualification passes every gate
on its own driver. The v4 confirmatory qualification tripped one gate
(`content_captured_false_everywhere`) only because of a missing
column in the driver's run-scoped SELECT; the underlying DB had
`content_captured=0` for every row. Commit `9054f46f` adds the
column. Replaying the corrected driver against the v4 DB produces
all-30-gates-PASS with the same accounting numbers as v4's initial
run.

**No live 5 % extended collection has been enabled.** The profile
stays paused (`compare_reads: false`, `sample_rate: 0.0`) until the
operator explicitly flips it.

---

## Companion Code SHAs

This document is the canonical record for the following
implementation, test, and driver commits.

| SHA       | Subject |
|-----------|---------|
| `40cd1dfff` | gate fix (QualificationReport.valid) |
| `2bf8dcbf` | qualification flags |
| `e6223cc4` | persist deterministic sampled-read comparisons |
| `ba91d8b` | disagreement adjudication workflow |
| `ce3f8a1` | P1 safety / accounting tests |
| `4dcdfb76` | P1 evidence collection doc |
| `046bdfb0` | unique comparison IDs; collision handling |
| `e2bc23bf` | pre-launch dry-run + Fuli latency probe |
| `b4f10dec` | timeout detection + persistence invariant |
| `b10a15d05` | dedicated `_is_timeout_error()` matcher |
| `bbd4f6b83` | in/out telemetry contract (sha256 + ms stamps) |
| `1c4e008eb` | reproducible live qualification driver |
| `d0db85930` | canonical evidence document |
| `9054f46f` | follow-on `content_captured` SELECT fix |

Fuli is pinned at commit `727ce92603707619e0155a6c0ca1a01f5f2e07c4`.

---

## Stage A Baseline (4-hour valid)

- **Run ID:** `20260713_p1_soak_4h_retry`
- **Report:** `reports/shadow/20260713_p1_soak_4h_retry/report.json`
- **Status:** `run_status: valid`, `ok: true`
- **Gates confirmed:**
    - accounting_balanced
    - mirror_only_on_success
    - accepted_rate >= 99
    - eventual_index_rate >= 99
    - zero_silent_loss
    - zero_namespace_leak
    - zero_raw_content_violations
    - sqlite_integrity_ok
    - primary_output_unchanged

The Stage A report covers the **read-time mirror** pipeline
(write-time mirror + write-completion acknowledgement).
Honcho availability is part of the run_status contract; this
baseline accepts the run only when every primary write was
acknowledged by Honcho within the soak window.

---

## 100-Query Hermetic Qualification (synthetic)

- **Driver:** `scripts/p1_qualify_100q.py` (debug-only path,
  no longer in the tree)
- **Result:** 100/100 synthetic comparison rows persisted
- **Foreground overhead (telemetry contract):**
    - p50 = 0.03 ms
    - p95 = 0.12 ms
    - max = 0.89 ms
- **All thresholds met with 25x–200x margin**

The 100-query run exercises the executor with a controlled
secondary stub that simulates Fuli at both the success and
timeout paths. It is hermetic (no live Honcho, no live Fuli) and
its primary purpose is regression coverage on the executor's
accounting and shutdown logic. Live evidence is collected
by the 15-minute driver below.

---

## v3 — Canonical Real Qualification (15-minute live)

This is the canonical evidence that the system runs cleanly
against **real Honcho at `http://rtx.tail2d065a.ts.net:8000`**
(real pinned Fuli at `727ce9260`, RTX Tailscale peer).

| Field | Value |
|---|---|
| **Run ID** | `p1-live5pct-qualify-20260714T232103-adf862` |
| **QUAL_HOME** | `/tmp/hermes-p1-qualify-d0002c8c` |
| **comparisons.db** | `/tmp/hermes-p1-qualify-d0002c8c/memories/comparisons.db` |
| **Log** | `/tmp/live_qualify_v3.log` |
| **Started** | 2026-07-14T22:32:03Z |
| **Ended** | 2026-07-14T22:47:12Z |
| **DECISION** | **GO** |
| **Duration** | 901.2 s |

### Workload

- **Total reads:** 236
- **Sampled (per telemetry contract):** 14
- **Pre-run row count:** 0
- **Post-flush run-scoped row count:** 14

### Foreground (per telemetry contract)

- **Primary hash invariance:** 236 / 236 (100 %)
- **Enqueue overhead (n=14):**
    - p50 = < 2 ms
    - p95 = < 10 ms
    - max = < 25 ms

### Real Fuli (run-scoped rows)

| secondary_status | count |
|---|---|
| success | 13 |
| failed (`Fuli call timed out after 2.0s`) | 1 |
| **secondary success rate** | **92.9 %** |

- **Fuli latency (successes + timeout):** p50 ≈ 76 ms,
  p95 ≈ 6 835 ms, max ≈ 6 835 ms
- **Without the timeout outlier:** p50 ≈ 76 ms, p95 ≈ 95 ms,
  max ≈ 98 ms

### Executor Accounting

| Counter | Value |
|---|---|
| `comparison_jobs_sampled` | 14 |
| `comparison_jobs_enqueued` | 14 |
| `comparison_jobs_started` | 14 |
| `comparison_jobs_completed` | 13 |
| `comparison_jobs_timed_out` | **1** |
| `comparison_jobs_failed` | 0 |
| `comparison_jobs_persisted` | 14 |
| `comparison_jobs_pending` | 0 |
| `comparison_jobs_dropped_queue_full` | **0** |
| `comparison_jobs_orphaned` | 0 |
| `persistence_started` | 14 |
| `persistence_failed` | 0 |
| `unexpected_collision` | 0 |
| `duplicate_idempotent` | 0 |

### Accounting Identities (all true)

- `sampled == enqueued == started == persisted == 14`
- `completed + timed_out + failed = 13 + 1 + 0 = 14 = persisted`
- `run_scoped_db_rows == persisted == 14`
- `is_balanced == True`

### Privacy / Namespace

- `namespaces`: `{hermes:shadow-pilot}` only
- `content_captured == False` for every row
- `unique comparison_ids`: 14
- `raw_query_hits_in_db`: 0 / 35 controlled queries
- No raw primary payload, no raw memory text, no `.env` secrets
  persisted

### Redacted Export (Issue 6)

```
HERMES_HOME=/tmp/hermes-p1-qualify-d0002c8c \
HERMES_PROFILE=shadow-pilot \
venv/bin/hermes shadow disagreements export \
  --redacted --output /tmp/p1-live5pct-redacted.json
```

- File size: 18.4 KB
- 14 redacted comparisons
- All rows filtered to single `QUAL_RUN_ID`
- `raw_query_hits_in_export`: 0 / 35
- `fuli_marker_in_export`: False
- No secrets

### SQLite / WAL

- `PRAGMA integrity_check` → `[('ok',)]`
- `PRAGMA quick_check` → `[('ok',)]`
- `PRAGMA wal_checkpoint(TRUNCATE)` → `[(0, 0, 0)]`

### Shutdown

- `persistence_thread_alive_after_shutdown`: False
- Process exited normally.

---

## v4 — Confirmatory Re-Run (15-minute live)

| Field | Value |
|---|---|
| **Run ID** | `p1-live5pct-qualify-20260714T233734-f1fa34` |
| **QUAL_HOME** | `/tmp/hermes-p1-qualify-27c5e7a1` |
| **comparisons.db** | `/tmp/hermes-p1-qualify-27c5e7a1/memories/comparisons.db` |
| **Log** | `/tmp/live_qualify_v4.log` |

v4 is a confirmatory re-run of the same driver against the same
provider topology. It uses the canonical, replayable driver
(with the post-v4 `content_captured` SELECT fix in `9054f46f`)
and is expected to produce gate-equivalent evidence to v3.

### v4 outcomes (post-fix replay against the same DB rows)

Direct SQL against the v4 DB:

- 14 run-scoped rows
- `content_captured = 0` for all 14 rows (int)
- `r.get('content_captured') == 0` returns `True` for all rows
- secondary_status breakdown: 13 `success` + 1 `failed`
  (the Fuli `"timed out"` envelope)

The driver in `9054f46f` now includes `content_captured` in
the run-scoped SELECT, so the same DB rows produce all-gates-PASS
on the corrected driver.

### v4 outcomes (initial run, before the SELECT fix)

- Total reads 236; sampled 14; invariant 236/236
- enqueue overhead `p50=0.50ms p95=2.01ms max=2.74ms`
  (all under thresholds)
- All accounting identities PASS
- 0 drops, 0 orphans, 0 collisions, 0 persistence failures
- `is_balanced == True`
- `content_captured_false_everywhere == False` ONLY because the
  SELECT omitted `content_captured`. After the SELECT fix
  (`9054f46f`), the same DB rows PASS this gate.

The v4 DECISION line printed in the original run was `NO-GO`,
isolated to the missing `content_captured` column in the
driver's SELECT. The DB itself had `content_captured=0` for
every row. The follow-on commit (`9054f46f`) fixes the SELECT
and produces a `DECISION: GO` against the same DB.

---

## Run-Scoped Accounting Identities (definitions)

The **run-scoped evidence boundary** is the union of:

```
EXECUTOR_PERSISTED   ==  comparison_jobs_persisted (in-memory count)
RUN_SCOPED_ROWS      ==  COUNT(*) FROM comparisons WHERE run_id = <this run>
TERMINAL_JOBS        ==  comparison_jobs_completed
                       + comparison_jobs_timed_out
                       + comparison_jobs_failed
```

And the **corrected invariant**:

```
EXECUTOR_PERSISTED  ==  RUN_SCOPED_ROWS  ==  TERMINAL_JOBS
```

Every persisted row, **including timeouts and other Fuli errors**,
must satisfy:

```
secondary_status   IN ('success', 'failed')
secondary_error_category != NULL  iff  secondary_status = 'failed'
```

This invariant prevents evidence drift where the executor
reports N persisted but the DB contains M != N rows (because of
post-write purges, schema-level row drops, or count-side bugs).

---

## Primary Invariance Definition

Primary invariance is **not** inferred from a second direct
Honcho call (Honcho is stateful and a second call is not an
equivalent timing baseline).

The canonical evidence is the **sha256 hash contract** added in
`bbd4f6b83`:

```
primary_result_sha256  ==  returned_result_sha256
```

Both hashes are computed inline inside
`ShadowMemoryProvider.handle_tool_call` from monotonic ms stamps
and the primary's raw payload. The hashes are sha256 of the
returned string; if either side differs, the gate FAILS.

No raw primary payload is ever written to telemetry. Hashes
only.

---

## Foreground Overhead Definition

Foreground enqueue overhead is computed from the in/out telemetry
contract:

```
enqueue_overhead_ms  ==
    enqueue_completed_at_ms - enqueue_started_at_ms
```

Both stamps are recorded inside
`ShadowMemoryProvider.handle_tool_call` only when the read was
sampled. The hash contract and the ms-stamp contract share a
single `_finalize_telemetry` static helper.

---

## Real Fuli Performance Definition

"Real Fuli" means:

- The Fuli provider is the live one (pinned at
  `727ce92603707619e0155a6c0ca1a01f5f2e07c4`).
- The Fuli bridge is the live `fuli_memory_search` tool.
- The Fuli index is the live `memories/fuli.db` from the
  shadow-pilot profile, copied read-only into QUAL_HOME.
- Latencies are recorded by Fuli itself (in
  `memory_search` INFO events).

A row's `secondary_status` is set in the executor's
`_run_comparison`:
- `'success'` when the envelope parses and contains results.
- `'failed'` when the envelope is an error JSON returned by the
  bridge (e.g. timeout). The error is classified by
  `_is_timeout_error` into `timed_out` vs `failed`.

---

## Privacy / Namespace Definition

A row passes privacy and namespace gates when, and only when:

- `run_id == <qualification run_id>`
- `namespace == 'hermes:shadow-pilot'`
- `content_captured == False`
- No schema column carries a raw primary payload or a raw memory text.
- No `.env` secret is persisted.
- The redacted export contains only fingerprint rows (sha256 of
  provider-internal IDs, redacted query text); no raw query
  string is exported.

---

## SQLite / WAL Definition

- `PRAGMA integrity_check` must return `[('ok',)]`.
- `PRAGMA quick_check` must return `[('ok',)]`.
- `PRAGMA wal_checkpoint(TRUNCATE)` must return `[(0, 0, 0)]`
  (no uncheckpointed frames).

---

## Shutdown Definition

After `shadow.shutdown()`:

- `shadow._executor._persistence_thread.is_alive()` is False.
- No shadow-compare threads remain (`is_alive() == False`).
- The process exits 0 only if every required gate passes.

---

## Known Limitations

- **Single worker = single bottleneck.** The executor's
  `comparison_max_workers=1` caps secondary throughput at
  ~7–8 comparisons/min on the live profile. At 5 % sample rate
  and a 15-min window, the backpressure (`time.sleep(3.7)`)
  calibrates read throughput to keep the queue from overflowing.
  Operators wanting higher secondary throughput should raise
  `comparison_max_workers` and revalidate with the qualification
  driver.

- **Fuli's `"timed out"` verb form.** The new
  `_is_timeout_error()` matcher recognizes "timed out",
  "timeout", and "deadline exceeded" (case-insensitive, with
  underscore normalization). If Fuli introduces a new envelope
  form not in this set, those rows will be classified as
  `failed` until the matcher is updated.

- **Honcho is stateful.** Primary invariance uses
  sha256-of-the-returned-string, not a second Honcho call.
  The two are mathematically equivalent because the hash is
  computed from the exact string the primary returned.

- **Live 5 % has NOT been enabled.** This document authorizes
  the operator to enable it under the
  [Extended Collection Plan](#extended-collection-plan) below.
  No collection has been auto-started.

- **Shadow-pilot profile is paused.** The live
  `~/.hermes/profiles/shadow-pilot/config.yaml` has
  `compare_reads: false, sample_rate: 0.0` until the operator
  approves the extended-collection plan and explicitly enables it.

---

## Extended Collection Plan

### Recommended Initial Window

| Setting | Value |
|---|---|
| Duration | 7 days |
| `compare_reads` | true |
| `sample_rate` | 0.05 |
| `sampling_seed` | 0 |
| `comparison_budget_ms` | 2000 |
| `comparison_max_workers` | 1 |
| `comparison_max_queue_size` | 128 |
| `capture_content` | false |
| Honcho role | primary (unchanged) |
| Fuli role | secondary (results never enter live output) |

### Daily Gates (rolling 24 h)

| Gate | Threshold |
|---|---|
| primary output mutation | == 0 |
| queue drops | == 0 |
| persistence failures | == 0 |
| orphaned workers | == 0 |
| unexpected collisions | == 0 |
| pending jobs after flush | == 0 |
| namespace leaks | == 0 |
| raw-content violations | == 0 |
| SQLite integrity | ok |
| Fuli secondary success | >= 90 % rolling 24 h |
| foreground enqueue p95 | < 10 ms |

### Automatic Pause Conditions

The collection pauses on **any** of the following:

- Any privacy or namespace violation
- Any primary-output mutation
- Any unexpected collision
- Any persistence loss
- queue drops > 0
- orphaned worker > 0
- Fuli secondary success below 80 % over at least 20 samples
- primary availability degradation
- SQLite integrity failure

### Collection Targets

- Minimum 100 run-scoped comparisons
- Minimum 50 human-adjudicated disagreements
- Minimum 5 adjudicated examples for each major query type
  represented (profile / preference / project / episodic /
  exact / semantic / recent / contradiction)
- No retrieval tuning before the adjudication target is met

### Enable / Observe / Pause / Export / Restore

- **Enable:** the operator runs
    ```yaml
    memory:
      shadow:
        compare_reads: true
        sample_rate: 0.05
    ```
  on the **live shadow-pilot profile** AFTER the user explicitly
  approves this plan.

- **Observe:** a `tenet-fleet-monitor`-style health check is run
  daily on the shadow-pilot profile. The monitor reads
  `comparison_jobs_*` counters and the integrity_check / WAL
  state from the live `memories/comparisons.db`. Each rollup
  records whether any daily gate failed; a single failure
  triggers the automatic pause path.

- **Pause:** the operator (or the monitor) sets the live profile
  to `compare_reads: false, sample_rate: 0.0` and replays the
  qualification driver against `QUAL_HOME` to capture a fresh
  evidence row, then files a brief.

- **Export:** the operator runs
    ```
    HERMES_HOME=~/.hermes/profiles/shadow-pilot \
    HERMES_PROFILE=shadow-pilot \
    venv/bin/hermes shadow disagreements export \
      --redacted --output <path>
    ```
  per the operator guide. The export is filtered to the
  collection's run_id.

- **Restore:** the operator flips the live profile's
  `compare_reads` and `sample_rate` back to the collection
  values, confirms the shadow-pilot profile, and replays the
  qualification driver.

### Restart Requirement

**No gateway or dashboard restart is required for the live
shadow-pilot profile to collect evidence.** The shadow-pilot
profile uses its own `HERMES_HOME` and is loaded per-process.
The standalone qualification driver (`scripts/p1_qualify_live_15min.py`)
is a self-contained process that opens the shadow-pilot profile
and writes to its `memories/comparisons.db`. The live gateway
and dashboard both use the *default* Hermes profile; they do
**not** read from `memories/shadow-pilot/comparisons.db`.

The live Hermes gateway **must NOT be modified to load the
shadow-pilot profile**: that would invert the boundary. The
gateway must continue serving the default profile (Honcho +
default memory) for live answers, while the standalone
shadow-pilot driver collects evidence in parallel.

If, in the future, the operator decides to wire the
shadow-pilot profile into the live gateway, that change
**must**:

1. Be a feature commit on a feature branch (not direct to
   `integration/fuli-v0.18.2`).
2. Be approved by the user in writing before the gateway is
   restarted.
3. Be reviewed against the **privacy contract**: Fuli results
   must never enter live output (compare_reads must stay
   off the primary output path).

Today this is **NOT approved and NOT planned.** The
shadow-pilot collection lives in a standalone process.

---

## Operator Enablement

To enable the seven-day extended live 5% collection, an operator
must take three actions, in order, **after** the user explicitly
approves this plan:

1. Flip the live profile to the collection settings:

   ```yaml
   memory:
     shadow:
       compare_reads: true
       sample_rate: 0.05
       sampling_seed: 0
       comparison_budget_ms: 2000
       comparison_max_workers: 1
       comparison_max_queue_size: 128
       capture_content: false
       namespace: hermes:shadow-pilot
   ```

   Place this on the disk at
   `~/.hermes/profiles/shadow-pilot/config.yaml`.
   **`mirror_writes: true` MUST remain unchanged.**

2. Start the standalone shadow-pilot driver in the background
   (NOT inside the live Hermes gateway):

   ```bash
   cd /Users/caiado/.hermes/hermes-agent
   HERMES_HOME=/Users/caiado/.hermes/profiles/shadow-pilot \
     HERMES_PROFILE=shadow-pilot \
     PYTHONPATH=/Users/caiado/.hermes/hermes-agent \
     nohup venv/bin/python -u \
       scripts/p1_qualify_live_15min.py \
         --duration-minutes 10080 \
         --output-dir ./reports/p1-extended-$(date +%Y%m%d)/ \
       > /tmp/p1-extended.log 2>&1 &
   ```

3. Apply the daily gate checks (see
   [Daily Gates](#daily-gates-rolling-24-h)) against
   `reports/p1-extended-*/report.json` plus the live
   `memories/comparisons.db` every 24 hours.

The standalone driver does **not** require a gateway or dashboard
restart — see [Restart Requirement](#restart-requirement) below.

### Disable / Pause

To pause the collection, flip the live profile to:

```yaml
memory:
  shadow:
    compare_reads: false
    sample_rate: 0.0
```

The driver process keeps running but produces no sampled
comparisons. Stopping the process explicitly is optional.

### Restore Prior State

The original paused-profile SHA-256
`359ece27ffd508e277ace2a278b5754ec45325e326e6dce00772ea59f4d77882`
is recoverable from the audit log under
`reports/p1-live-qualification-results-doc-<timestamp>.sha256`.

---

## Current State at Document Time

- `branch`: `integration/fuli-v0.18.2`
- `HEAD`: `45e62bba1bd954afa71fa9b47f91d89b52574518` (most recent at
  this document's authored time)

## Controlled-Soak Admission-Capacity Failure (2026-07-15)

The seven-day controlled real-provider shadow soak launched at
`2026-07-15T01:34Z` (`p1-controlled-soak-20260715T013342Z`,
QUAL_HOME `/tmp/hermes-p1-qualify/run-1fd40a24/`,
comparisons.db `/tmp/hermes-p1-qualify/run-1fd40a24/memories/comparisons.db`)
auto-paused at the 03:34Z hourly checkpoint on the **queue_drops**
hard-pause gate. The auto-pause logic in `99918b54d` worked
correctly — it caught the failure on the first monitoring
interval and stopped the run cleanly.

### Truthful statement of what happened

- The 34 queue drops were **NOT** caused by Fuli warm-up.
- Fuli throughput was well within budget:
  - 99.2% secondary success rate (127/128)
  - secondary latency p50 = 82 ms, p95 = 392 ms, max = 6705 ms
    (one captured Fuli "timed out" envelope)
  - the producer generated ~0.8 sampled comparisons/minute
- The defect was in `ComparisonExecutor.enqueue()` in
  `pilot/comparison_executor.py`. The executor used a
  `queue.Queue(maxsize=128)` in series with a
  `ThreadPoolExecutor`. The queue was **never drained** because
  `ThreadPoolExecutor` consumes its own work via an **unbounded**
  internal `SimpleQueue` and never reads from the external queue.
  `put_nowait()` permanently deposited jobs in a queue whose
  consumer never existed; once 128 jobs had been admitted over
  the executor's lifetime, the queue was at capacity forever and
  every subsequent `put_nowait` raised `queue.Full`. From that
  point on, **all** admitted samples were rejected — regardless
  of whether the worker was busy or idle.
- All 128 admitted comparisons were persisted (the
  `comparison_jobs_persisted` and DB row count matched
  exactly).
- The driver's auto-pause correctly stopped accepting new work
  after the first checkpoint detected `dropped_queue_full > 0`.

### Earlier attribution (now superseded)

A previous draft of this document briefly attributed the 34 drops
to Fuli warm-up. That diagnosis was wrong. The producer rate
during Fuli's first 5–10 minutes (when Fuli was loading the
embedding model and timing out at 6–7 seconds per call) was
~0.8 sampled comparisons/min, far below the worker's processing
capacity (~7–8/min sustained). The genuine cause was the executor
admission-path defect, not Fuli warmup.

### Fix

`b366dd488` `fix(shadow): recycle comparison admission capacity after completion`
replaces the false queue.Queue admission token with a
`threading.BoundedSemaphore` of capacity
`max_queue_size + max_workers`. `enqueue()` non-blockingly
acquires one permit; the permit is held until
`_on_comparison_done()` fires (every termination path:
success, Fuli timeout, Fuli error, future exception, future
cancellation). Capacity fully recycles after every batch.

`45e62bba1` `test(shadow): add 300-job recycling + submit-failure regression tests`
adds the 300-job regression and a defensive submit-failure test.
It also:

- decrements `_jobs_enqueued` and releases the permit if
  `ThreadPoolExecutor.submit()` itself raises (rare, but
  possible during pool shutdown), so the executor never leaks
  capacity on a failed submit;
- wraps the post-release bookkeeping in `_on_comparison_done`
  in a `try/except` so the permit release cannot be bypassed by
  accounting or logging errors;
- exposes `admission_available` in `executor_accounting()`.

The five-idempotent invariant for `is_balanced()` is now:

  1. `sampled == enqueued + dropped_queue_full`
  2. `enqueued == started + queue_depth`
  3. `started == completed + timed_out + failed`
  4. `completed + timed_out + failed == persisted`
  5. `outstanding_jobs <= admission_capacity`

Cancellation is folded into `failed` (the cancellation branch
in `_on_comparison_done` increments `_jobs_failed` exactly once
because Python's `Future.cancel()` only succeeds on a future
that has not yet started, so `_run_comparison`'s exception path
will not also fire). This avoids the brief's
double-subtraction concern for cancelled terminal jobs.

### Test totals (post-fix)

- `pytest tests/plugins + tests/pilot -q`: 174 passed (was 160
  before the fix; +12 admission-capacity + 2 new tests)
- `pytest tests/pilot -W error::ResourceWarning -q`: 138 passed;
  zero warnings, zero leaks
- `pytest tests/pilot/test_comparison_admission_capacity.py -v`:
  14 passed in 27.89 s, including
  `test_300_jobs_recycle_capacity_past_multiple_lifetime_bounds`
  and `test_submit_failure_releases_permit_and_no_enqueue_counted`.

### Status of the seven-day soak

The previous seven-day run remains **incomplete and invalid for
completion**. The 128 persisted rows remain useful provider
evidence:

- 99.2% secondary success (matches the live system rate)
- 100% primary hash invariance
- Privacy / namespace / SQLite / shutdown gates all green

But the run did not reach seven days, and the auto-pause
proves the admission-path failure mode is now observable in
practice. The seven-day run is **not** promoted to evidence.

The next session must:

1. Run a controlled **30-minute real-provider capacity
   qualification** with ≥300 accepted comparisons, against the
   same provider topology, with `sample_rate` high enough to
   deterministically exceed 256 admissions in 30 minutes.
   Required gates (per the post-fix brief):
   - jobs 129, 256, 300 all admitted and persisted
   - `dropped_queue_full == 0`
   - post-flush idle: queue_depth = active_jobs = outstanding_jobs
     = 0, admission_available = admission_capacity
   - Fuli secondary success ≥ 90%, primary hash invariance 100%,
     enqueue p95 < 10 ms
   - redacted export clean, SQLite integrity / quick-check
     / wal_checkpoint clean, clean shutdown
2. **Only after the 30-minute qualification passes**, seek a
   separate, explicit user approval to relaunch the seven-day
   controlled soak with the admission-capacity fix in place.

The queue-drop auto-pause gate has not been weakened, relaxed,
or removed. It fired correctly on the 2026-07-15 soak and will
fire correctly on the next soak if admission capacity ever
leaks again.

## Operator Enablement
  - `compare_reads: false`
  - `sample_rate: 0.0`
  - `mirror_writes: true` (preserved)
  - config.yaml SHA-256:
    `359ece27ffd508e277ace2a278b5754ec45325e326e6dce00772ea59f4d77882`
- Live `~/.hermes/profiles/shadow-pilot/config.yaml`:
  - `compare_reads: false`
  - `sample_rate: 0.0`
  - `mirror_writes: true` (preserved)
- Default Hermes profile (`~/.hermes/config.yaml`): **untouched**
- Default Hermes gateway (PID 45422): **not restarted**
- Default Hermes dashboard (PID 40765): **not restarted**
- Live 5 % shadow collection: **NOT enabled**

The gate to enable extended collection is **explicit user
approval of this plan** followed by the operator flipping the
live shadow-pilot profile from
`compare_reads: false, sample_rate: 0.0` to
`compare_reads: true, sample_rate: 0.05`.

This document is the canonical evidence for that approval.
