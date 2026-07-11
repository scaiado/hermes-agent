# P0 Reliability and Measurement Integrity Plan

## Goal
Fix accounting, classification, and traceability in the Hermes ↔ Fuli shadow pilot before any further soak. Do not enable another overnight pilot until every attempt is accounted for.

## Repositories
- Hermes: `/Users/caiado/.hermes/hermes-agent`
- Fuli: `/Users/caiado/repos/Fuli-Memory-Core`

## Evidence preserved
`/Users/caiado/.hermes/hermes-agent/reports/shadow/overnight-001/`
- `pilot.log`
- `overnight_pilot.log`
- `pilot_smoke.log`
- `post_pilot.log`
- `pilot_metrics.jsonl`
- `fuli.db` (+ WAL/SHM)
- `config.redacted.yaml`
- `diagnostics.json`
- `process_status.txt`
- `cli_memory_status.txt`

## Root causes to fix
1. **Primary classification is binary and wrong.** `do_one_write` only checks `parsed.get("error")` or `parsed.get("status") == "error"`. Honcho may return structured success shapes that don't match this heuristic, causing false negatives.
2. **Mirror ignores primary success.** `_mirror_write` always records `primary_success=True` even when primary failed. The shadow wrapper calls `_mirror_write` unconditionally after every primary call.
3. **No correlation IDs.** No way to tie a primary write to a Fuli memory.
4. **No indexing drain.** Pilot finishes while Fuli still has pending indexing.
5. **No real shadow CLI.** `run_cli_checks` calls `hermes --profile shadow-pilot shadow preflight` which doesn't exist; it falls through to `hermes memory status`.
6. **Report lifecycle is broken.** Post-processor ran before pilot finished, used stale in-process report, and committed files outside the repo.
7. **No cleanup command.** Synthetic memories can't be removed by metadata.

## Implementation steps

### Hermes side
1. Add `pilot/ledger.py` — canonical per-attempt ledger with invariants.
2. Rewrite `pilot/shadow_pilot.py` to:
   - generate correlation IDs per operation
   - classify primary results using a dedicated classifier
   - mirror only confirmed-success primary writes
   - record every attempt in the ledger
   - perform bounded indexing drain at completion
   - verify accounting balances before generating report
   - run real CLI checks only after subprocess exit
3. Update `plugins/memory/shadow/__init__.py` to:
   - accept per-call correlation IDs and pilot metadata
   - mirror only when explicitly told the primary succeeded
   - propagate correlation IDs into Fuli metadata
   - expose `_report` and `_drain` helpers
4. Update `plugins/memory/shadow/shadow_store.py` to:
   - add correlation_id, pilot_run_id, pilot_sequence, metadata columns
   - provide ledger-friendly query methods
   - redact content in reports
5. Add `plugins/memory/shadow/cli.py` real subcommands:
   - `preflight`, `status`, `report`, `cleanup`, `validate-report`
6. Add tests in `tests/plugins/test_shadow_provider.py` and `tests/pilot/test_shadow_pilot.py`.
7. Add `docs/honcho-superiority-scorecard.md`.

### Fuli side
1. Add explicit accepted/indexed/pending/failed state to `add` return value.
2. Add batch status lookup by memory IDs.
3. Add `await_indexed(memory_ids, timeout_seconds)` helper.
4. Add correlation-ID metadata filtering in `search`/`get`.
5. Add WAL/integrity diagnostics.
6. Add tests for the above.

### Validation
1. Deterministic integration test with fake Honcho and fake embedder.
2. 15-minute real validation (15s interval, write-only, synthetic tags, drain).
3. Only then consider a 4-hour soak.

## Go/no-go criteria
- No unexplained attempts in any report.
- Primary classification internally consistent.
- Mirror only on primary success.
- Accepted Fuli writes ≥99%.
- Eventual Fuli indexing ≥99% after drain.
- Zero namespace/privacy violations.
- All tests green.
