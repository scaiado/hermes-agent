# Hermes ↔ Fuli Integration Plan

## Objective

Add Fuli as a **selectable local memory provider** for Hermes, and build a **safe shadow-mode pilot** where Honcho remains primary and Fuli receives mirrored writes / sampled comparison reads without affecting live answers.

## Scope

- Do not change the user's normal `~/.hermes/config.yaml`.
- Do not switch the normal Hermes profile away from Honcho.
- Do not run destructive tests against existing Honcho or Fuli data.
- No live network or model download in tests.

## A. Direct Fuli provider (selectable)

**Risk: medium** — this is a new plugin path, but it is opt-in and local-only.

1. Verify plugin files exist and load correctly.
2. Refactor to lazy embedder initialization (no model download at discovery/startup).
3. Harden async bridge: per-provider instance, bounded timeouts, safe shutdown.
4. Register in `hermes_cli/memory_providers.py` for desktop/setup UI.
5. Add unit/integration tests with a fake embedding provider.
6. Run isolated smoke test with a temporary Hermes home.

## B. Honcho-primary / Fuli-shadow provider

**Risk: high** — any mistake here could change live memory behavior or expose duplicate tools.

1. Create a separate `plugins/memory/shadow/` plugin that wraps primary + secondary providers.
2. Preserve Honcho tool schemas and behavior exactly.
3. Mirror writes to Fuli best-effort, never failing the caller.
4. Sample reads from Fuli after the Honcho result is returned, compare fingerprints, never inject Fuli results into live answers.
5. Add a local SQLite evidence store for observations and reports.
6. Add safe config defaults (all shadow features disabled by default).
7. Add tests proving disabled shadow preserves Honcho behavior, Fuli failure never changes primary output, no duplicate tools.

## Configuration

Direct provider:

```yaml
memory:
  provider: "fuli"
  fuli:
    db_path: ""
    namespace: "hermes:default"
    embedding_provider: "local"
    embedding_model: ""
    device: "cpu"
    ollama_host: ""
    lazy_init: true
    timeout_ms: 5000
```

Shadow provider:

```yaml
memory:
  provider: "shadow"
  shadow:
    primary_provider: "honcho"
    secondary_provider: "fuli"
    enabled: false
    mirror_writes: false
    compare_reads: false
    sample_rate: 0.0
    timeout_ms: 250
    capture_content: false
    namespace: "hermes:default"
```

## Quality gates

- `ruff check` on changed files
- `pytest` on new tests
- Isolated direct-Fuli smoke test
- Shadow-disabled regression test
- Fake Honcho + temporary Fuli shadow smoke test
- `git status` and small logical commits before any push

## Out of scope

- Modifying the Fuli repository itself (unless a genuine contract gap is found).
- Enabling Fuli in the user's normal profile.
- Pushing without verifying remotes and workflow.
