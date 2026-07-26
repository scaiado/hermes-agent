# Fuli Seven-Day Qualification Report

## Verdict: **FAIL** (one hard gate breached)

The seven-day controlled real-provider shadow soak
(RUN_ID `p1-controlled-soak-20260716T123125Z`, started
2026-07-16T12:39:43Z) was **auto-paused** on
2026-07-22T01:43:35Z after 5 days, 13 hours, 4 minutes of
runtime. The auto-pause was triggered by the hard gate
`fuli_secondary_success_below_80pct`. The run was not
allowed to reach its natural 7-day duration. **A new
qualification run is required before the canary gate
opens.**

## Final accounting

| Counter | Value |
|---------|------:|
| `comparison_jobs_sampled` | 7,177 |
| `comparison_jobs_enqueued` | 7,177 |
| `comparison_jobs_started` | 7,177 |
| `comparison_jobs_completed` | 5,737 |
| `comparison_jobs_timed_out` | 3 |
| `comparison_jobs_failed` | 1,437 |
| `comparison_jobs_dropped_queue_full` | 0 |
| `comparison_jobs_persisted` | 7,177 |
| `comparison_jobs_pending` | 0 |
| `comparison_jobs_orphaned` | 0 |
| `comparison_jobs_unexpected_collision` | 0 |
| `queue_depth` | 0 |
| `active_jobs` | 0 |
| `outstanding_jobs` | 0 |
| `persistence_thread_alive_after_shutdown` | false |
| `is_balanced` | true |

## Identity checks (all pass except success rate)

| Check | Result |
|-------|--------|
| `sampled_equals_enqueued` | 7,177 == 7,177 ✅ |
| `enqueued_equals_started` | 7,177 == 7,177 ✅ |
| `completed + timed_out + failed == persisted` | 5,737 + 3 + 1,437 == 7,177 ✅ |
| `run_scoped_db_rows_equals_persisted` | 7,177 == 7,177 ✅ |
| `queue_drops_zero` | true ✅ |
| `persistence_failures_zero` | true ✅ |
| `orphaned_zero` | true ✅ |
| `pending_zero_after_flush` | true ✅ |
| `unexpected_collisions_zero` | true ✅ |
| `duplicate_idempotent_zero` | true ✅ |
| `is_balanced` | true ✅ |
| `namespace_isolated` | true ✅ |
| `content_captured_false_everywhere` | true ✅ |
| `raw_query_not_in_db` | true ✅ |
| `redacted_export_ok` | true ✅ |
| `raw_query_not_in_export` | true ✅ |
| `no_fuli_marker_in_export` | true ✅ |
| `export_filtered_to_run_id` | true ✅ |
| `persistence_thread_dead_after_shutdown` | true ✅ |
| `integrity_check_ok` | true ✅ |
| `quick_check_ok` | true ✅ |
| `wal_checkpoint_clean` | true ✅ |
| `secondary_success_at_least_80pct` | **false** ❌ |

The success rate was 5,737 / 7,177 = **79.96%**, which is
0.04 percentage points below the 80% threshold. The
auto-pause gate fired correctly; the system did not paper
over the failure.

## Success-rate analysis

- 1,437 of 7,177 comparisons failed (20.04% failure rate).
- 3 of 7,177 timed out (0.04% timeout rate — the Fuli
  per-call timeout classification works correctly).
- The bulk of the failures are real Fuli
  timeouts/responses classified as `failed` rather than
  `timed_out`. The 80% threshold was set to gate on the
  real-world secondary provider's reliability, not on
  infrastructure. A real Fuli rate of 80% is the agreed
  floor; a 79.96% rate indicates a real-world signal, not
  a measurement artifact.

## Privacy verification (all pass)

- Only the `hermes:shadow-pilot` namespace was used.
- `content_captured` is false for every persisted row.
- No raw query text is present in the DB or in the
  redacted export.
- No API keys, secrets, or environment values appear in
  any artifact.

## SQLite verification (all pass)

- `integrity_check`: ok
- `quick_check`: ok
- `wal_checkpoint(TRUNCATE)`: clean

## Shutdown verification

- Persistence thread is not alive after shutdown.
- `is_balanced()` is true.
- All enqueued jobs reached a terminal state
  (completed / timed_out / failed).
- Outstanding jobs = 0.

## Source profile state (verified at soak auto-pause time)

- `compare_reads`: `false`
- `sample_rate`: `0.0`
- `mirror_writes`: `true`

The source profile remained paused throughout the run.

## Evidence manifest

The full evidence directory is at
`/Users/caiado/.hermes/hermes-agent/reports/p1-controlled-soak-20260716T123125Z/`.
This includes:

- `report.json` — final report with the full accounting
  summary and the auto-pause decision.
- `driver.log` — driver log with the final test runner
  output.
- 133 checkpoint files in `checkpoints/`.
- `LAUNCH_NOTES.md` — launch notes.
- `redacted_disagreements.json` — redacted export of 7,177
  comparison rows.
- The comparison DB at
  `/tmp/hermes-p1-qualify/run-bc2a17e2/memories/comparisons.db`
  (~2.6 MiB).

## Hashes

(SHA-256; see `reports/product/fuli-seven-day-qualification.json`
for the machine-readable manifest.)

## Conclusion

The qualification run is FAIL because the Fuli secondary
success rate fell below the 80% hard gate threshold. The
infrastructure (executor, persistence, balance, privacy,
SQLite integrity, namespace isolation) all PASS. The
failure is in the **real-world Fuli provider** response
reliability, not in the Hermes or Fuli-product
infrastructure.

A new qualification run is required. The cause of the
20% failure rate (network, Fuli rate limit, Fuli version
mismatch) must be investigated and remediated before
re-running. The new run will be a separate, explicitly
authorized mission; this report does not authorize it.

**The seven-day qualification is FAIL. No canary, no tag,
no deploy.**
