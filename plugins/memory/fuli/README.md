# Fuli Memory Provider (Hermes plugin)

**Status:** local pilot / opt-in  
**Scope:** Adds Fuli as a selectable memory provider inside Hermes. Fuli remains a separate local canonical-memory project; this directory is only the Hermes adapter.

## What it does

- Stores Hermes memories in a local SQLite database (`<HERMES_HOME>/memories/fuli.db` by default).
- Indexes memories with vector + lexical hybrid search.
- Supports namespaces, lifecycle, reinforcement, and diagnostics.
- Defers all heavy work (embedder build, SQLite migrations, vector index) until the first tool call, so plugin discovery and schema listing stay fast.

## Installation

Fuli is imported as an editable install from the local checkout. The Hermes venv must contain:

```bash
uv pip install -e /Users/caiado/repos/Fuli-Memory-Core
```

(Replace the path with your local Fuli checkout.)

## Configuration

Config lives in `~/.hermes/config.yaml` under `memory.fuli`, or in `~/.hermes/fuli/config.json` for the desktop UI.

```yaml
memory:
  provider: "fuli"
  fuli:
    db_path: ""                         # default: <HERMES_HOME>/memories/fuli.db
    namespace: "hermes:default"
    embedding_provider: "local"         # only local is implemented today
    embedding_model: ""                 # empty = Fuli default sentence-transformer
    device: "cpu"                       # cpu | mps | cuda
    lazy_init: true                     # strongly recommended
    timeout_ms: 5000
```

## Lazy initialization

`is_available()` only imports the `fuli` package — it does not download or load a model. The first `fuli_memory_*` call triggers provider construction. This keeps `hermes memory setup` and agent startup fast.

## Direct-provider warning

When `memory.provider` is set to `fuli`, the model sees the `fuli_memory_*` tools and uses Fuli directly. There is no shadow protection; only enable this in a dedicated test profile.

## Smoke test

A fake-embedder smoke test runs in a temporary `HERMES_HOME`:

```bash
cd /Users/caiado/.hermes/hermes-agent
.venv/bin/python tests/manual/fuli_direct_smoke.py
```

No network or model download is required.

## Troubleshooting

- **Model download on first call:** If you did not set a fake embedder, the first call downloads a `sentence-transformers` model. This is expected and should not be counted as provider latency.
- **Timeouts:** Lower `timeout_ms` only if you have a fast local machine; on first call the model load and index build can take several seconds.
- **Namespace leakage:** Use distinct namespaces per profile to keep memories isolated.
