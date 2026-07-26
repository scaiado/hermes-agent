# Fuli Canary 1% — Operator Runbook

This runbook is the exact procedure for the operator to execute
when a separately approved 1% Fuli-primary canary mission is
authorized. It does not authorize the canary itself; it is the
procedure for after such authorization is given.

The runbook is tested offline against copied config. It has
not been executed against a live profile.

## Preconditions

Before any operator action, the following must be true:

- [ ] Seven-day qualification PASS report present at
      `docs/product/fuli-seven-day-qualification.md`.
- [ ] Non-inferiority gate PASS at
      `docs/product/fuli-noninferiority.md`.
- [ ] Source profile `~/.hermes/profiles/shadow-pilot/config.yaml`
      backed up; SHA-256 recorded in the runbook log.
- [ ] Default profile `~/.hermes/config.yaml` backed up;
      SHA-256 recorded.
- [ ] Fuli health check PASS (no error on `is_available`).
- [ ] Honcho health check PASS.
- [ ] Rollback drill executed against a copied config in the
      last 24 hours; rollback log shows the mode transition
      off -> shadow -> off cleanly.
- [ ] Operator is present and reachable.

The operator records the SHA-256 of every config touched before
the canary. The recording is appended to the runbook log.

## Enable command (do not execute until authorized)

```
hermes memory fuli install --mode canary --canary-percentage 1 \
  --canary-salt <operator-chosen-salt> \
  --fuli-allowlist <operator-chosen-allowlist> \
  --honcho-allowlist <operator-chosen-control-list>
```

The `--canary-salt` is chosen by the operator and recorded in
the runbook log. The salt is what makes the cohort
assignment deterministic; the same salt + same user_id always
produces the same cohort.

## Monitoring cadence

- **0:00–0:05** — first 5 minutes: verify the canary router
  produced a `RouteDecision` for every request and the
  returned provider matches the cohort. Use:
  `hermes memory fuli status` to inspect state. The Reporter's
  `state` field must remain `healthy`.
- **0:05–0:15** — 15 minutes: verify Fuli success rate
  remains >= 99% and no hard gate has fired. Inspect:
  `reports/product/fuli-canary-telemetry.json` (rotated
  every 15 minutes by the runtime).
- **0:15–1:00** — 1 hour: same as above plus verify p95
  latency remains under 500 ms. If p95 exceeds 500 ms for any
  5-minute window, escalate.
- **1:00–6:00** — 6 hours: verify privacy counters are zero
  (no `raw_content_leak`, no `namespace_violation`). Verify
  Honcho fallback count is non-zero only if Fuli had at least
  one failure (Fuli success rate should be ~99% in this
  window).
- **6:00–24:00** — 24 hours: verify the auto-rollback gate
  count is zero. Verify the returned-provider provenance is
  recorded on every response (per the `RouteDecision`).

## Hard rollback command

The hard rollback command is:

```
hermes memory fuli disable --reason "<operator-chosen-reason>"
```

This is a centralized transition via `_transition_mode`; the
`previous_mode` is preserved, the runtime is shut down
first, the config is written atomically with a backup, and
the mode becomes `off`.

For a partial rollback to a prior safe mode (e.g. from canary
to adjudicated_shadow):

```
hermes memory fuli pause --reason "<reason>"
hermes memory fuli enable adjudicated_shadow \
  --fuli-allowlist <current-allowlist> \
  --honcho-allowlist <current-control-list>
```

## Automatic rollback gates

The product auto-pauses on any of the following canonical
hard-gate keys:

- `primary_mutation`
- `namespace_violation`
- `raw_content_leak`
- `persistence_failure`
- `queue_drop`
- `orphaned_worker`
- `unexpected_collision`
- `sqlite_integrity_failure`
- `secondary_success_below_threshold`

Legacy spellings (`primary_mutations`, `namespace_violations`,
`persistence_failed`, `queue_drops`, `dropped_queue_full`,
`orphaned`, `secondary_success_rate_low`) are normalized to
the canonical set by `normalize_accounting()` and
`normalize_hard_gates()`.

## Evidence to record

For every response during the canary, the `RouteDecision`
carries:

- returned-provider provenance (provider name)
- cohort (fuli / honcho / kill_switch / allowlist)
- deterministic hash
- user_id (privacy-safe; no payload)
- mode (canary)
- canary_percentage
- allowlisted flag

The canary telemetry JSONL file (one line per decision) is
written to `reports/product/fuli-canary-telemetry.jsonl` and
contains no raw query or payload fields.

## Stop conditions

The operator MUST stop the canary (run the hard rollback
command) if any of the following occur:

- Any privacy event (`raw_content_leak`,
  `namespace_violation`, or any field carrying
  `secret`/`password`/`api_key`).
- Primary-output corruption: the returned provider's
  response fails the byte-for-byte Honcho invariant for
  any test query.
- Persistent error spike: Fuli success rate < 95% for two
  consecutive 5-minute windows.
- Rollback failure: the hard rollback command itself fails
  to converge the executor to a balanced state within 60
  seconds.
- p95 latency > 1s for any 10-minute window.
- Any contradiction: a recorded adjudication (operator-side)
  identifies Fuli as the cause of a contradiction regression
  versus Honcho.

## Backup procedure

Before any operator action, the operator runs:

```
hermes memory fuli backup --label "canary-pre"
```

This writes `~/.hermes/backups/fuli-product/<label>-<timestamp>.yaml`
and prints the SHA-256. The operator records the SHA-256 in
the runbook log.

To restore:

```
hermes memory fuli restore --label "canary-pre"
```

Restoration is byte-identical: the operator can verify by
re-hashing the restored file and comparing to the recorded
SHA-256.
