# Shadow Memory Provider (Hermes pilot)

**Status:** safe comparison pilot  
**Risk level:** low when `enabled: false`; experimental when enabled.

## What it does

The shadow provider keeps **Honcho as the authoritative memory source** while silently mirroring some writes to and sampling reads from **Fuli**. Fuli never changes the answer the model receives; it is only used for comparison and evidence collection.

## How it is safe

- Honcho tool schemas are preserved exactly. The model does not see Fuli tools.
- All primary calls return their native result unchanged, byte-for-byte.
- Fuli failures are caught, logged, and recorded; they never fail a caller.
- Secondary calls use a strict timeout (default 250 ms) so a slow Fuli cannot stall the agent.
- The evidence store keeps only SHA-256 fingerprints and latency numbers. Raw query text or memory content is not stored unless you explicitly configure `capture_content: true`.

## Configuration

```yaml
memory:
  provider: "shadow"
  shadow:
    primary_provider: "honcho"
    secondary_provider: "fuli"
    enabled: false          # master switch; leave false to behave like Honcho alone
    mirror_writes: false
    compare_reads: false
    sample_rate: 0.0        # 0.0-1.0 fraction of reads to compare
    timeout_ms: 250
    capture_content: false
    namespace: "hermes:default"
```

When `enabled` is false, the shadow provider is a transparent wrapper around the primary provider.

## Evidence store

Observations are kept in `<HERMES_HOME>/memories/shadow.db`:

- `mirrored_writes` — outcomes of write-mirroring attempts.
- `observations` — read comparison samples (latencies, overlap@1/3/5, missing primary fingerprints, error categories).

Only hashes are stored; the actual memory text or query is not retained.

## CLI commands

Available only when `memory.provider: shadow` is active:

```bash
hermes shadow status
hermes shadow report
hermes shadow compare --query "..."
hermes shadow namespace [--set hermes:default]
```

## Isolated smoke test

```bash
cd /Users/caiado/.hermes/hermes-agent
.venv/bin/python tests/manual/shadow_smoke.py
```

This uses a fake Honcho primary and a fake-embedder Fuli in a temporary `HERMES_HOME`.

## Rollback

Set `memory.shadow.enabled: false` and restart the session. No data migration is needed because Honcho remains the only authoritative source.

## Known limitations

- Only `honcho_conclude` and `honcho_profile` writes are mirrored today.
- Only `honcho_search` reads are compared.
- Config schema is flat; nested UI fields may need manual YAML editing.
- The Fuli package must be installed in the Hermes venv; it is not an automatic dependency.
