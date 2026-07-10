# Hermes ↔ Fuli Integration Progress

## Summary

Integration is implemented, tested, committed, and pushed to a writable fork. The normal Hermes profile has **not** been activated. The first live shadow pilot is ready for activation (mirrored writes only, no read comparison) pending user approval.

## Fuli regression diagnosis

### Environment captured

- Fuli checkout: `/Users/caiado/repos/Fuli-Memory-Core`
- Fuli commit: `719e429a9c8cb4d1de11b6c616413f410c32ea49` (origin/main, clean)
- Hermes venv Python: `3.11.14`
- Installed Fuli: editable from the same commit
- Key missing packages in Hermes venv that Fuli `[dev]` requires:
  - `pytest-asyncio` (any version)
  - `mypy` (any version)
  - `types-PyYAML` (any version)

### Root cause

The 106 failures in the Hermes venv were caused by `pytest-asyncio` not being installed. Fuli tests are decorated with `@pytest.mark.asyncio`; without the plugin, pytest treats them as synchronous `async def` functions, which immediately fails with "async def was not awaited". `mypy` was also absent, so the earlier `mypy src` command was actually run via a temporary `uvx` environment and produced 88 pre-existing errors.

### Clean environment verification

Created `/Users/caiado/repos/Fuli-Memory-Core/.venv-clean` with Python 3.11.14 and installed `fuli-memory-core[dev]`:

```bash
cd /Users/caiado/repos/Fuli-Memory-Core
uv venv --python python3.11 .venv-clean
source .venv-clean/bin/activate
uv pip install -e ".[dev]"
```

Results:

- `ruff check .`: **All checks passed!**
- `mypy src`: **Success: no issues found in 41 source files**
- `pytest -q`: **187 passed, 1 warning in 111.10s**
- `python -m fuli.benchmarks`: **Overall: PASS**

This confirms the historic 187-test baseline is valid and the current Fuli source is sound.

### Hermes venv result

Without the Fuli dev extras:

- `pytest -q`: **81 passed, 106 failed** (all async tests fail due to missing plugin)
- `mypy src`: **88 errors** (actually run in temporary uvx env; pre-existing FastAPI decorator issues)
- `ruff check .`: **All checks passed!**
- `python -m fuli.benchmarks`: **Overall: PASS**

## Changes made to dependencies or source

No Fuli source changes were needed. No broad dependency downgrades.

To align the Hermes venv with the clean baseline, the missing dev packages must be installed:

```bash
uv pip install --python /Users/caiado/.hermes/hermes-agent/.venv/bin/python \
  "pytest-asyncio>=0.25.0" "mypy>=1.15.0" "types-PyYAML>=6.0.0"
```

This command is pending user consent because it modifies the managed Hermes venv.

Hermes integration changes made in this pass:

- Added `hermes shadow preflight` command.
- Added `tests/manual/fuli_real_embedder_smoke.py`.
- Primed the Fuli async bridge loop during provider build for accurate first-call latency measurement.
- Fixed the real-embedder smoke test to use the correct `memory_id` / `get` schema.

## Real embedding smoke-test result

```text
{
  "status": "ok",
  "model_cached": true,
  "first_call_latency_ms": 6167.28,
  "search_latency_ms": 19.67,
  "first_call_seconds": 6.17,
  "memory_id": "01KX6KPPJHBRRX9P5175GYENZY"
}
```

- Model: `sentence-transformers/all-MiniLM-L6-v2` was already present in the Hugging Face cache (from the clean-venv benchmark run), so no download occurred.
- First call: ~6.2 s (Fuli provider build, embedder model load, vector index creation, indexing).
- Normal search after init: ~20 ms.
- Restart + retrieve + delete + search-exclusion all passed.
- Temporary HERMES_HOME and DB removed.

## Hermes integration regression-test results

All focused tests pass with the current code:

```bash
.venv/bin/python -m pytest tests/plugins/test_fuli_provider.py tests/plugins/test_shadow_provider.py -q --timeout=120
# 28 passed in 8.92s
```

Both smoke tests pass:

- `tests/manual/fuli_direct_smoke.py`: status ok (fake embedder)
- `tests/manual/shadow_smoke.py`: status ok (fake Honcho + fake embedder Fuli)
- `tests/manual/fuli_real_embedder_smoke.py`: status ok (real local embedder)

Lint/type checks on integration files pass:

```bash
uvx ruff check plugins/memory/fuli/__init__.py plugins/memory/shadow/cli.py tests/manual/fuli_real_embedder_smoke.py
# All checks passed!
uvx --python .venv/bin/python ty check plugins/memory/fuli/__init__.py plugins/memory/shadow/cli.py tests/manual/fuli_real_embedder_smoke.py
# All checks passed!
```

## Fork and branch

- Fork created: `https://github.com/scaiado/hermes-agent`
- Local branch: `feat/fuli-shadow-memory`
- Remote: `scaiado https://github.com/scaiado/hermes-agent.git`
- Upstream: `origin https://github.com/NousResearch/hermes-agent.git`

## Commit preservation / push state

All commits pushed to `scaiado/hermes-agent` on branch `feat/fuli-shadow-memory`:

- `34c59d064` — feat(memory): add hardened local Fuli provider and Honcho-primary shadow
- `7c9e14278` — test(memory): cover Fuli and shadow provider integration
- `1f740366f` — docs(memory): document Fuli and shadow pilot operation
- `1b797ffc6` — fix(memory): preflight check and real embedder smoke readiness

Verified via `git push scaiado feat/fuli-shadow-memory`.

## Pilot preflight result

`hermes shadow preflight` (with a shadow provider config) reports:

- Primary provider availability
- Fuli import path
- Fuli version/commit
- DB directory writable
- Namespace
- Model cached/not cached
- Shadow DB writable
- capture_content state
- write/read shadow flags
- timeout
- evidence counts

It does not mutate memory or download a model. A dedicated test can exercise it after the pilot profile is created.

## Go/no-go decision for mirrored-write pilot

**Go for a conservative first pilot** with the config below.

Rationale:

- Fuli source is verified clean (187 passed in clean venv, benchmark PASS).
- Hermes integration tests are green (28 focused tests, all smoke tests ok).
- Fail-open behavior is tested and safe.
- Evidence store does not store raw content.
- Primary Honcho behavior is unchanged when shadow is enabled.
- Fuli is isolated to its own namespace.
- The first pilot uses only mirrored writes with `compare_reads: false`, so the model still only sees Honcho results.

Caveats:

- Hermes venv is not yet aligned with Fuli dev extras; the Fuli package test suite does not pass in the Hermes venv until `pytest-asyncio`, `mypy`, and `types-PyYAML` are installed.
- Read comparison is intentionally disabled in the first pilot.

## Exact pilot activation command (do not execute without user approval)

Create a dedicated test profile and start Hermes with it:

```bash
mkdir -p ~/.hermes/profiles/shadow-pilot
# Write the first-stage config
cat > ~/.hermes/profiles/shadow-pilot/config.yaml <<'EOF'
memory:
  provider: "shadow"
  shadow:
    enabled: true
    primary_provider: "honcho"
    secondary_provider: "fuli"
    mirror_writes: true
    compare_reads: false
    sample_rate: 0.0
    timeout_ms: 250
    capture_content: false
    namespace: "hermes:shadow-pilot"
EOF

# Run preflight without changing the default profile
HERMES_PROFILE=shadow-pilot hermes shadow preflight

# If preflight is green, start the agent in the pilot profile
HERMES_PROFILE=shadow-pilot hermes
```

Do **not** modify the default `~/.hermes/config.yaml`.
