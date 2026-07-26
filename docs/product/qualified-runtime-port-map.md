# Qualified Fuli Runtime — Port Map

This document classifies every qualified-runtime artifact that must move
from `integration/fuli-v0.18.2` (HEAD `cfb9535cd`) to the compatibility
branch `integration/fuli-product-current-main` (base `origin/main` HEAD
`78c06525e`).

## Source branch
- Branch: `integration/fuli-v0.18.2`
- SHA: `cfb9535cd38243988d0298771350c048885cf375`

## Target branch
- Branch: `integration/fuli-product-current-main`
- Base: `origin/main` @ `78c06525e8e955e06a007b07b347c679f3977c3e`
- Python: 3.11+ (worktree-local `.venv`)

## Per-file classification

### Pilot runtime (required — class A port)
| File | Source | Target | Conflicts? | Notes |
|------|--------|--------|------------|-------|
| `pilot/comparison_executor.py` | cfb9535c | copy | none (new file on main) | Bounded executor with semaphore admission |
| `pilot/comparison_store.py` | cfb9535c | copy | none | SQLite WAL store |
| `pilot/comparison_metrics.py` | cfb9535c | copy | none | |
| `pilot/ledger.py` | cfb9535c | copy | none | |
| `pilot/primary_classifier.py` | cfb9535c | copy | none | |
| `pilot/sampling.py` | cfb9535c | copy | none | |
| `pilot/shadow_pilot.py` | cfb9535c | copy | none | |

### Memory plugins (required — class A port)
| File | Source | Target | Conflicts? | Notes |
|------|--------|--------|------------|-------|
| `plugins/memory/fuli/__init__.py` | cfb9535c | copy | none (new dir on main) | Imports `fuli.*` package; runtime only when Fuli installed |
| `plugins/memory/fuli/plugin.yaml` | cfb9535c | copy | none | |
| `plugins/memory/fuli/README.md` | cfb9535c | copy | none | |
| `plugins/memory/shadow/__init__.py` | cfb9535c | copy | none | Imports `pilot.*` (will be present) and `plugins.memory.load_memory_provider` (present in main) |
| `plugins/memory/shadow/plugin.yaml` | cfb9535c | copy | none | |
| `plugins/memory/shadow/README.md` | cfb9535c | copy | none | |
| `plugins/memory/shadow/shadow_store.py` | cfb9535c | copy | none | |

### Memory provider discovery (already in main)
| File | Source | Target | Conflicts? | Notes |
|------|--------|--------|------------|-------|
| `plugins/memory/__init__.py` | cfb9535c | keep main | yes (minor) | Main has `list_memory_provider_names` and `encoding="utf-8"` already; no merge needed |

### Memory provider base (already in main)
| File | Source | Target | Conflicts? | Notes |
|------|--------|--------|------------|-------|
| `agent/memory_provider.py` | cfb9535c | keep main | none | Identical between branches |

### Pilot tests (required — class A port)
| File | Source | Target | Conflicts? | Notes |
|------|--------|--------|------------|-------|
| `tests/pilot/__init__.py` | cfb9535c | create | none | |
| `tests/pilot/test_comparison_admission_capacity.py` | cfb9535c | copy | none | |
| `tests/pilot/test_comparison_executor.py` | cfb9535c | copy | none | |
| `tests/pilot/test_ledger.py` | cfb9535c | copy | none | |
| `tests/pilot/test_p1_comparison_pipeline.py` | cfb9535c | copy | none | |
| `tests/pilot/test_primary_classifier.py` | cfb9535c | copy | none | |
| `tests/pilot/test_primary_health_gate.py` | cfb9535c | copy | none | |
| `tests/pilot/test_shadow_pilot.py` | cfb9535c | copy | none | |

### Plugin tests (required — class A port)
| File | Source | Target | Conflicts? | Notes |
|------|--------|--------|------------|-------|
| `tests/plugins/test_fuli_provider.py` | cfb9535c | copy | none (new file on main) | Will skip on missing `fuli` package |
| `tests/plugins/test_shadow_provider.py` | cfb9535c | copy | none | |

### Product package (apply after qualified runtime is present)
| Commit | Source | Notes |
|--------|--------|-------|
| `29bc8af0b` (Phase 5) | `product/fuli-memory-v1` branch | Cherry-pick or three-way merge onto compat branch after pilot/ is present |

### Files NOT ported (omit)
- None — the qualified Fuli runtime is small and self-contained.
- `pilot/__init__.py` if present: keep main's version.

## Strategy
- File-level port (option B from the prompt) because the qualified branch
  diverged 1319 commits from main and cherry-picking 171 commits would
  carry unrelated drift.
- Provenance is recorded per file in this map; tests pin behavior to
  invariant assertions (accounting identities, admission capacity, etc.)
  rather than snapshots.

## Invariants preserved
- Semaphore admission with capacity recycling
- Timeout classification (real Fuli timeout envelopes)
- Foreground output invariance
- Persistence worker lifecycle
- Flush/shutdown ordering
- Privacy contract (no raw query, no provider payload in persisted row)
- Accounting identities (sampled = enqueued + dropped; enqueued = started + queue_depth; etc.)
