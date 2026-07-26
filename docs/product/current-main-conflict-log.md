# Current-Main Compatibility Conflict Log

The compat branch `integration/fuli-product-current-main` is based on
`origin/main` HEAD `78c06525e8e955e06a007b07b347c679f3977c3e`. The
qualified Fuli runtime was ported from `integration/fuli-v0.18.2` HEAD
`cfb9535cd38243988d0298771350c048885cf375`. The product package was
applied from `product/fuli-memory-v1` HEAD
`29bc8af0b0f1de9a35cf7fa66581fc9df4750faf`.

## Conflicts encountered

### None blocking

The qualified runtime, the product package, and the current main
were applied as additive file drops. No line-level merge conflicts
occurred. The compat worktree was created from a fresh
`git worktree add` of `origin/main`; the qualified and product files
were applied with `git checkout <source> -- <path>` (file-level port),
which does not engage the merge machinery.

## Structural integration gaps (not conflicts, but unresolved)

### Gap 1: `hermes_cli/main.py` does not register the qualified CLI subcommands
- **Where:** `hermes_cli/main.py` on `origin/main` does not contain
  imports of `hermes_cli.subcommands.shadow.build_shadow_parser` or
  `hermes_cli.subcommands.disagreements.build_disagreements_parser`.
- **Why:** Those `build_*_parser` symbols exist on the qualified
  branch's `main.py` (commit `c65b381a0` and later). On `origin/main`
  the CLI dispatcher's choice sets are different.
- **Impact:** The qualified CLI subcommands (`hermes shadow ...`,
  `hermes disagreements ...`) are importable as Python modules but
  are NOT reachable through the standard `hermes` entrypoint on
  `origin/main` without further wiring.
- **Phase 5 action:** None. The compat branch defers CLI dispatcher
  integration to a follow-up PR that targets the upstream
  `hermes_cli/main.py` choice-set expansion.
- **Why deferred:** The product/qualified mission is memory-side;
  the dispatcher touches the entire CLI surface and is owned by
  upstream. Submitting that change in a feature branch risks a
  long review cycle unrelated to the Fuli integration goal.

### Gap 2: `hermes memory` subcommand has a static choice set
- **Where:** `hermes_cli/main.py` (origin/main) registers the
  `memory` subcommand with `choices=["setup", "status", "off",
  "reset"]` and provider enumerations from
  `list_memory_provider_names()`.
- **Why:** `list_memory_provider_names()` is the upstream discovery
  mechanism. It returns 11 providers on the compat branch
  (`byterover, fuli, fuli_product, hindsight, holographic, honcho,
  mem0, openviking, retaindb, shadow, supermemory`).
- **Impact:** The product's `memory_fuli.py` is reachable as a
  Python module but is NOT registered as `hermes memory fuli`
  through the standard entrypoint. Users can invoke
  `python -m hermes_cli.subcommands.memory_fuli ...` directly, or
  the dispatcher can be widened in a follow-up PR.
- **Phase 5 action:** Documented; the compat branch ships the
  module anyway so the production-readiness path can wire it
  without re-issuing the compat PR.

### Gap 3: `pilot/__init__.py` is implicit
- **Where:** the qualified branch's `pilot/` directory has no
  `__init__.py` on the compat worktree either.
- **Why:** the qualified branch relied on namespace packages.
- **Impact:** None observed; all 158 qualified tests pass under
  the compat worktree.
- **Action:** None.

## Behavioral diffs that did NOT cause conflicts

### `plugins/memory/__init__.py`
- The qualified branch (cfb9535c) has the `read_text()` call
  without an explicit `encoding=` argument. The compat branch
  (78c06525e, which is `origin/main`) has `encoding="utf-8"`.
- **Why:** ruff PLW1514 enforcement.
- **Action:** Kept the `origin/main` version. The qualified runtime
  imports from `plugins.memory` via the module-level
  `load_memory_provider` symbol, which is present in both branches.

### `agent/memory_provider.py`
- Identical between cfb9535c and 78c06525e.
- **Action:** No port needed.

## File-level port provenance

Every ported file carries the upstream provenance in
`docs/product/qualified-runtime-port-map.md`. Re-porting is a
deterministic `git checkout cfb9535c -- <path>` operation against
any future qualified branch.

## Re-runnable verification

`python reports/product/run-compat-matrix.py` exercises all eight
scenarios A through H and writes
`reports/product/phase5-compatibility-matrix.json` plus
`docs/product/phase5-compatibility-matrix.md`. Current run: 8/8
PASS.
