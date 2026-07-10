# Hermes ↔ Fuli Integration Progress

## Summary

Integration is implemented, tested, committed, and pushed to a writable fork. The normal Hermes profile has **not** been activated. A conservative mirrored-write shadow pilot is ready but **not enabled**.

## Environment boundary (final)

| Environment | Path | Purpose | Fuli dev extras |
|---|---|---|---|
| Fuli development / quality gates | `/Users/caiado/repos/Fuli-Memory-Core/.venv-clean` | `pytest -q`, `mypy src`, `ruff check .`, `python -m fuli.benchmarks` | Yes (`pytest-asyncio`, `mypy`, `types-PyYAML`) |
| Hermes runtime / integration | `/Users/caiado/.hermes/hermes-agent/.venv` | Hermes integration tests (`tests/plugins/test_fuli_provider.py`, `tests/plugins/test_shadow_provider.py`), runtime smoke tests (`tests/manual/*.py`), `hermes shadow preflight` | No (intentionally) |

**Decision:** Fuli dev extras are intentionally absent from the Hermes venv. The earlier 106 test failures in the Hermes venv were caused by running Fuli's development suite (`pytest` with `@pytest.mark.asyncio` tests) in a runtime-only environment that lacked `pytest-asyncio`. They were **not** caused by Fuli source regressions.

## Fuli clean-venv baseline verification

- Fuli commit: `719e429a9c8cb4d1de11b6c616413f410c32ea49` (origin/main, clean)
- `ruff check .`: **All checks passed!**
- `mypy src`: **Success: no issues found in 41 source files**
- `pytest -q`: **187 passed, 1 warning in 111.10s**
- `python -m fuli.benchmarks`: **Overall: PASS**

## Hermes integration gates (re-run in the Hermes venv)

```bash
cd /Users/caiado/.hermes/hermes-agent
.venv/bin/python -m pytest tests/plugins/test_fuli_provider.py tests/plugins/test_shadow_provider.py -q --timeout=120
# 28 passed in 4.87s

.venv/bin/python tests/manual/fuli_direct_smoke.py
# { "status": "ok", ... }

.venv/bin/python tests/manual/shadow_smoke.py
# { "status": "ok", ... }

.venv/bin/python tests/manual/fuli_real_embedder_smoke.py
# { "status": "ok", "model_cached": true, "first_call_latency_ms": 6299.79, ... }

uvx ruff check plugins/memory/fuli/__init__.py plugins/memory/shadow/cli.py tests/manual/fuli_real_embedder_smoke.py
# All checks passed!

uvx --python .venv/bin/python ty check plugins/memory/fuli/__init__.py plugins/memory/shadow/cli.py tests/manual/fuli_real_embedder_smoke.py
# All checks passed!
```

All focused integration tests and smoke tests pass in the Hermes venv without any Fuli dev extras installed.

## Real embedding smoke-test result

```text
{
  "status": "ok",
  "model_cached": true,
  "first_call_latency_ms": 6299.79,
  "search_latency_ms": 14.83,
  "first_call_seconds": 6.3,
  "memory_id": "01KX6N0G434C4W89JYTGYEJV0E"
}
```

- Model: `sentence-transformers/all-MiniLM-L6-v2` (already in HF cache).
- First call: ~6.3 s (provider build + model load + vector index + indexing).
- Normal search after init: ~15 ms.
- Restart, retrieve, delete, and search-exclusion all passed.
- Temporary HERMES_HOME and DB were removed.

## `hermes shadow preflight` result

Run in a temporary HERMES_HOME with `memory.provider: shadow` (so Honcho is not configured; `Primary available: False` is expected there).

```text
Shadow memory preflight
────────────────────────────────────────
  Hermes home:        /var/folders/31/ywbhxb8x4s31jqlf_g5n04zw0000gn/T/hermes-shadow-pilot-byxnhv46
  Primary:            honcho
  Secondary:          fuli
  Enabled:            True
  Mirror writes:      True
  Compare reads:      False
  Sample rate:        0.0
  Timeout:            250 ms
  Capture content:    False
  Namespace:          hermes:shadow-pilot
  Primary available:  False
  Fuli import path:   /Users/caiado/repos/Fuli-Memory-Core/src/fuli/__init__.py
  Fuli version:       unknown
  Fuli commit:        719e429a9c8cb4d1de11b6c616413f410c32ea49
  Model cached:       True (sentence-transformers/all-MiniLM-L6-v2)
  DB dir writable:    /var/folders/31/ywbhxb8x4s31jqlf_g5n04zw0000gn/T/hermes-shadow-pilot-byxnhv46/memories
  Shadow config dir:  /var/folders/31/ywbhxb8x4s31jqlf_g5n04zw0000gn/T/hermes-shadow-pilot-byxnhv46/shadow
  Evidence store:     not created yet
```

- The `shadow` subcommand is discovered correctly when the active provider is `shadow`.
- Fuli is importable from the expected path.
- The Fuli commit matches the clean baseline.
- Model cache is warm.
- DB and shadow config directories are writable.
- No evidence store exists until the first mirrored write, as intended.

## Changes made

- Added `hermes shadow preflight` command.
- Added `tests/manual/fuli_real_embedder_smoke.py`.
- Primed the Fuli async bridge loop during provider build for accurate first-call latency measurement.
- Fixed the real-embedder smoke test to read `get_result["id"]` directly instead of the incorrect `results` nesting.
- Updated `progress.md` and `TODO.txt` with the environment boundary and final state.

No Fuli source changes were needed. No broad dependency downgrades were made.

## Fork and branch

- Fork: `https://github.com/scaiado/hermes-agent`
- Branch: `feat/fuli-shadow-memory`
- Remote: `scaiado https://github.com/scaiado/hermes-agent.git`
- Upstream: `origin https://github.com/NousResearch/hermes-agent.git`

## Commit history (pushed to `scaiado/hermes-agent`)

- `34c59d064` — feat(memory): add hardened local Fuli provider and Honcho-primary shadow
- `7c9e14278` — test(memory): cover Fuli and shadow provider integration
- `1f740366f` — docs(memory): document Fuli and shadow pilot operation
- `1b797ffc6` — fix(memory): preflight check and real embedder smoke readiness
- `06cceda0c` — docs(memory): update progress and TODO after regression diagnosis and pilot prep

## Pilot status

**Not activated.** The default Hermes profile is unchanged. A first-stage mirrored-write pilot is documented in `TODO.txt` and earlier in this file. Activation requires an explicit user decision and the creation of a dedicated `shadow-pilot` Hermes profile.

Recommended first-stage config:

```yaml
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
```

Do not modify `~/.hermes/config.yaml` directly.

## Remaining user-controlled items

- Go/no-go decision for the mirrored-write pilot.
- Activation of the dedicated `shadow-pilot` profile.
