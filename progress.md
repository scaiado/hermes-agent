# Hermes ↔ Fuli Integration Progress

## Pilot launch aborted — Honcho primary unreachable

The conservative shadow-pilot was **not launched** for the planned 4–8 hour window. The preflight static checks passed, but a dynamic write probe against the Honcho primary failed with `Connection refused` to `http://100.77.121.91:8000`. Per the pilot protocol, a non-functional primary is a hard stop.

### Preflight checks (passed)

```text
Shadow memory preflight
────────────────────────────────────────
  Hermes home:        /Users/caiado/.hermes/profiles/shadow-pilot
  Primary:            honcho
  Secondary:          fuli
  Enabled:            True
  Mirror writes:      True
  Compare reads:      False
  Sample rate:        0.0
  Timeout:            250 ms
  Capture content:    False
  Namespace:          hermes:shadow-pilot
  Primary available:  True
  Fuli import path:   /Users/caiado/repos/Fuli-Memory-Core/src/fuli/__init__.py
  Fuli version:       unknown
  Fuli commit:        719e429a9c8cb4d1de11b6c616413f410f32ea49
  Model cached:       True (sentence-transformers/all-MiniLM-L6-v2)
  DB dir writable:    /Users/caiado/.hermes/profiles/shadow-pilot/memories
  Shadow config dir:  /Users/caiado/.hermes/profiles/shadow-pilot/shadow
  Evidence store:     not created yet
```

### Dynamic primary probe (failed)

```text
Honcho session 'hermes-agent' add_peers failed (non-fatal): Connection failed: [Errno 61] Connection refused
Honcho session 'hermes-agent' loaded (failed to fetch context: Connection failed: [Errno 61] Connection refused)
Failed to create conclusion: Connection failed: [Errno 61] Connection refused
Failed to set peer card: Connection failed: [Errno 61] Connection refused
```

- Endpoint: `http://100.77.121.91:8000` (from `~/.hermes/profiles/shadow-pilot/honcho.json`)
- Symptom: host is reachable at network level, but port `8000` is not listening.
- Impact: every Honcho write tool returns a primary error, which is user-visible and violates the pilot success gates.

### Validation-run evidence (short 0.05h bursts before abort)

Two 3-minute validation runs were executed to confirm Fuli behavior while the primary was failing. The results are **not** a valid pilot, but they confirm the shadow provider mechanics:

- **Writes attempted:** 36 (across two runs)
- **Fuli writes succeeded:** 43 / 54 (79.6% aggregate)
- **Fuli writes failed:** 11 / 54 (20.4% aggregate)
- **Fuli timeouts:** 0 (timeout was 250 ms; first-call model load exceeded it, then writes succeeded)
- **Primary errors:** 36/36 (100% — all Honcho connection refused)
- **Namespace leak:** False
- **Raw-content violations:** 0
- **Thread/loop health:** alive at end, never dead
- **Fuli DB final size:** ~1.77 MB (after ~36 mirrored writes)
- **Fuli diagnostics:** 36 active memories in namespace `hermes:shadow-pilot`, all indexed, 0 pending/failed

The Fuli failures were contained in the evidence store and did not affect the returned primary result; however, the primary result itself was an error, so the user-visible outcome was still a failure.

### Blocker action required

Before relaunching the pilot, the Honcho primary endpoint at `http://100.77.121.91:8000` must be reachable and accept writes. Do not relaunch until a single `honcho_conclude` or `honcho_profile` call in the `shadow-pilot` profile returns a successful primary result.

## Environment boundary (final)

| Environment | Path | Purpose | Fuli dev extras |
|---|---|---|---|
| Fuli development / quality gates | `/Users/caiado/repos/Fuli-Memory-Core/.venv-clean` | `pytest -q`, `mypy src`, `ruff check .`, `python -m fuli.benchmarks` | Yes (`pytest-asyncio`, `mypy`, `types-PyYAML`) |
| Hermes runtime / integration | `/Users/caiado/.hermes/hermes-agent/.venv` | Hermes integration tests (`tests/plugins/test_fuli_provider.py`, `tests/plugins/test_shadow_provider.py`), runtime smoke tests (`tests/manual/*.py`), `hermes shadow preflight` | No (intentionally) |

## Fuli clean-venv baseline verification

- Fuli commit: `719e429a9c8cb4d1de11b6c616413f410f32ea49` (origin/main, clean)
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

## `hermes shadow preflight` result (dedicated profile)

Run with `--profile shadow-pilot`.

- `Primary available: True` (static config check)
- `Fuli commit: 719e429...`
- `Model cached: True`
- `DB dir writable: True`
- `compare_reads: False`, `sample_rate: 0.0`, `capture_content: False`

## Changes made

- Added `pilot/shadow_pilot.py` load generator and evidence collector.
- Added dynamic primary probe to the pilot script; aborts if the first Honcho write fails.
- Created dedicated `shadow-pilot` profile at `~/.hermes/profiles/shadow-pilot` without modifying `~/.hermes/config.yaml`.
- Updated `progress.md` and `TODO.txt` with the abort evidence and blocker.

## Fork and branch

- Fork: `https://github.com/scaiado/hermes-agent`
- Branch: `feat/fuli-shadow-memory`

## Pilot status

**Aborted.** The default Hermes profile is unchanged. The dedicated `shadow-pilot` profile exists but is not active. The pilot will not be relaunched until the Honcho primary endpoint is confirmed reachable and writing successfully.
