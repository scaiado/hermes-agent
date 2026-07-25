# Fuli Memory Product Runtime — Reuse Audit

## Audit scope

Compare the new `fuli_product` modules with the already qualified runtime in the
frozen checkout:

- `hermes-agent/pilot/comparison_executor.py`
- `hermes-agent/pilot/comparison_store.py`
- `hermes-agent/pilot/comparison_metrics.py`
- `hermes-agent/pilot/sampling.py`
- `hermes-agent/plugins/memory/shadow/__init__.py`
- `hermes-agent/plugins/memory/shadow/shadow_store.py`

## Reuse decisions

| New module | Qualified counterpart | Decision | Reason |
|---|---|---|---|
| `fuli_product.executor.ComparisonExecutor` | `pilot.comparison_executor.ComparisonExecutor` | **Reuse via adapter** | Qualified implementation is proven over 4+ day soak; has bounded semaphore admission, WAL SQLite store, orphan accounting, and tested shutdown. Do not maintain a second executor. |
| `fuli_product.store.ComparisonStore` | `pilot.comparison_store.ComparisonStore` | **Reuse via adapter** | Qualified schema with fingerprints, metrics, adjudication fields, and SQLite integrity. Reuse, not duplicate. |
| `fuli_product.sampling` | `pilot.sampling` | **Adapter / wrapper** | Qualified sampling has deterministic seed, query hash, decision bucket. Product layer can use it for config-driven sampling. |
| `fuli_product.config` | `plugins.memory.shadow.__init__` | **Extract and centralize** | Shadow provider reads legacy config. The product config is the new canonical source; shadow provider can migrate from it or be replaced by product orchestrator. |
| `fuli_product.lifecycle` | Hermes CLI / config helpers | **Adapter** | Pure business logic in core; Hermes config I/O in `fuli_product/adapters/hermes_config.py`. |
| `fuli_product.health` | `plugins.memory.shadow` + checkpoints | **New but data-driven** | Product-level health is new; it derives from qualified accounting fields, no new core logic. |
| `fuli_product.reporting` | `pilot.comparison_store` + checkpoint JSON | **Adapter / wrapper** | Reads qualified store and emits product status schema. |
| `fuli_product.evaluation` | `pilot.comparison_store.adjudication` | **Adapter** | Reuse comparison store adjudication schema; add product evaluation corpus. |

## Consequences

- Delete `fuli_product/executor.py` standalone implementation.
- Delete `fuli_product/store.py` standalone SQLite schema.
- Create `fuli_product/adapters/pilot_executor.py` that wraps the qualified executor.
- Create `fuli_product/adapters/pilot_store.py` that wraps the qualified store.
- Keep `fuli_product/config.py` as the canonical schema, but make it import-safe without `hermes_cli.config`.
- Move Hermes config I/O to `fuli_product/adapters/hermes_config.py`.

## Risk of not reusing

The qualified executor has been running for 4+ days in the controlled soak with
the admission-capacity fix (`BoundedSemaphore`). A second implementation would
need to re-qualify boundedness, WAL integrity, timeout accounting, and shutdown
behavior. Defaulting to reuse is the only responsible choice.

## Implementation plan

1. Remove standalone `fuli_product/executor.py` and `fuli_product/store.py`.
2. Create adapters that import qualified modules at runtime (not at import
   time, to keep `fuli_product` importable without full Hermes init).
3. Provide pure in-memory tests for product logic using fake adapters.
4. Add one integration test that exercises the qualified executor + store in a
   temp directory under Python 3.11.
5. Delete duplicate `sampling.py` logic that overlaps with `pilot.sampling`.
6. Update `__init__.py` to re-export from adapters.

## Acceptance criteria

- `import fuli_product` succeeds without importing `hermes_cli.config`.
- Product tests run in a temp directory with no live config mutation.
- Qualified suite regression tests still pass.
- No per-timeout unbounded thread growth; the qualified executor relies on
  Fuli's own async timeout, not Python thread killing.
- A migration path from legacy `memory.shadow` to `memory.fuli_product` is
  documented and tested on temporary files only.
