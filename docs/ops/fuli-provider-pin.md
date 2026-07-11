# Fuli-Memory-Core P0 baseline

Hermes `integration/fuli-latest-upstream` was validated against Fuli-Memory-Core
commit 727ce92603707619e0155a6c0ca1a01f5f2e07c4 (branch feat/p0-reliability).
The Fuli adapter was aligned to the P0 contract:

- `add_with_status` / `add_batch` for writes
- `batch_status` / `await_indexed` for indexing status
- `run_storage_health_check` for health checks

# Pin file

This file should be updated only when the Fuli-Memory-Core P0 contract changes.
