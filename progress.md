# Hermes ↔ Fuli Integration Progress

## Summary

Integration is implemented, tested, and committed locally. The normal Hermes profile has **not** been activated; the next pass will enable the shadow pilot in a dedicated test profile.

## Quality-gate commands used

```bash
# Hermes focused tests
.venv/bin/python -m pytest tests/plugins/test_fuli_provider.py -q --timeout=120
.venv/bin/python -m pytest tests/plugins/test_shadow_provider.py -q --timeout=120
.venv/bin/python -m pytest tests/plugins -q --timeout=120
.venv/bin/python -m pytest -q --timeout=120

# Lint / type (via uvx to avoid polluting the managed venv)
uvx ruff check plugins/memory/fuli/__init__.py plugins/memory/shadow/__init__.py plugins/memory/shadow/shadow_store.py plugins/memory/shadow/cli.py tests/plugins/test_fuli_provider.py tests/plugins/test_shadow_provider.py tests/manual/fuli_direct_smoke.py tests/manual/shadow_smoke.py hermes_cli/memory_providers.py
uvx --python .venv/bin/python ty check plugins/memory/fuli/__init__.py plugins/memory/shadow/__init__.py plugins/memory/shadow/shadow_store.py plugins/memory/shadow/cli.py tests/plugins/test_fuli_provider.py tests/plugins/test_shadow_provider.py tests/manual/fuli_direct_smoke.py tests/manual/shadow_smoke.py hermes_cli/memory_providers.py

# Smoke tests
.venv/bin/python tests/manual/fuli_direct_smoke.py
.venv/bin/python tests/manual/shadow_smoke.py

# Fuli package independent gates (from /Users/caiado/repos/Fuli-Memory-Core)
source /Users/caiado/.hermes/hermes-agent/.venv/bin/activate
ruff check .
mypy src
pytest -q
python -m fuli.benchmarks
```

## Results

### Hermes focused tests

- `tests/plugins/test_fuli_provider.py`: **16 passed**
- `tests/plugins/test_shadow_provider.py`: **12 passed**
- `tests/plugins -q`: **1668 passed, 52 failed** (failures are in Photon platform, Discord runtime, xAI video-gen tests; unrelated to memory integration)
- `pytest -q` full suite: collection errors in `tests/gateway/relay/test_relay_going_idle.py` and `tests/gateway/relay/test_ws_transport.py`; run timed out after 600 s when those were excluded. The full suite is not feasible to completion in this environment.

### Lint / type

- `ruff check` on integration files: **All checks passed!**
- `ty check` on integration files: **All checks passed!**
- `git diff --check`: no whitespace errors.

### Smoke tests

- `tests/manual/fuli_direct_smoke.py`: **status ok** (temporary HERMES_HOME, fake embedder, temp files removed).
- `tests/manual/shadow_smoke.py`: **status ok** (fake Honcho primary, fake-embedder Fuli, evidence store populated, fail-open verified, temp files removed).

### Fuli package independent gates

- `ruff check .`: **All checks passed!**
- `mypy src`: **88 errors** — all pre-existing (FastAPI decorator typing, missing uvicorn stubs), unrelated to Hermes integration.
- `pytest -q`: **81 passed, 106 failed** — failures concentrated in Fuli sync subsystem (sync observability, queue, wiring, worker, walking skeleton). These are pre-existing and unrelated to the Hermes adapter.
- `python -m fuli.benchmarks`: **Overall: PASS**

## Discrepancies found and fixed

- Reverted out-of-scope changes to `agent/chat_completion_helpers.py` and `tools/mcp_tool.py` (unrelated MCP PATH and flash fallback edits).
- Removed accidental untracked files `EOF` and `2-Areas/`.
- Added `_embedder` test hook and fixed `_inject_embedder` to avoid assigning a lambda over a method, which also silenced `ty` diagnostics.
- Added explicit tests for evidence-store privacy (no raw content), byte-for-byte primary result preservation, and no duplicate tools when shadow is enabled.
- Added `.gitignore` rules for `*.db`, `*.db-wal`, `*.db-shm`, `fuli/config.json`, `shadow/config.json`, `.env`.

## Commits

- `34c59d064` — feat(memory): add hardened local Fuli provider and Honcho-primary shadow
- `7c9e14278` — test(memory): cover Fuli and shadow provider integration
- `d7aa572ee` — docs(memory): document Fuli and shadow pilot operation

## Push state

- Remote: `https://github.com/NousResearch/hermes-agent.git` (origin)
- Branch: `main`
- Push attempt: **denied with 403** for user `scaiado`. This is the upstream repository; direct push is not permitted from this account.
- No push performed. Commits are local.

## Known limitations

- Only `honcho_conclude` and `honcho_profile` writes are mirrored.
- Only `honcho_search` reads are sampled and compared.
- Shadow config is currently edited via YAML; nested UI fields may need manual updates.
- Fuli must be installed as an editable package in the Hermes venv; it is not an automatic Hermes dependency.
- The full Hermes suite has unrelated pre-existing failures and collection errors; focused memory tests pass.

## Next safe activation command

Do not run this automatically. When the user explicitly approves the pilot, create or edit a dedicated test profile (e.g. `~/.hermes/profiles/shadow-test/config.yaml`) and set:

```yaml
memory:
  provider: "shadow"
  shadow:
    enabled: true
    primary_provider: "honcho"
    secondary_provider: "fuli"
    mirror_writes: true
    compare_reads: true
    sample_rate: 0.01
    timeout_ms: 250
    capture_content: false
    namespace: "hermes:shadow-test"
```

Do **not** modify the default `~/.hermes/config.yaml`.
