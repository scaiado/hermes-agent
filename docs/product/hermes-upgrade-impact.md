# Hermes Upgrade Impact Assessment — Fuli Memory Productization

## Executive Summary

| Aspect | Current Runtime | Pending Update | Risk |
|---|---|---|---|
| Hermes package version | `0.18.2` | `0.19.0` | Low |
| Git tag | none (integration branch) | `v2026.7.20` | — |
| Mainline commit | `7b5ba205` (integration merge-base) | `d7b36070e` | — |
| Fuli integration commits | 29 on `integration/fuli-v0.18.2` | not yet in main | Medium (merge) |
| Python runtime | unchanged | unchanged | Low |
| Desktop app | unchanged | new keep-awake / settings panel | Low |
| Gateway lifecycle | unchanged | checkpoint path fixes | Low |
| Memory-provider API | stable | Honcho client timeout staleness fix | Low |
| Config schema | `_config_version: 33` observed | `_config_version: 33` | Low |
| Breaking changes | none observed | none expected | Low |
| **Overall recommendation** | **safe with adaptation** | | |

## 1. Version Matrix

| Artifact | Current | Pending | Source |
|---|---|---|---|
| `pyproject.toml` version | `0.18.2` | `0.19.0` | `pyproject.toml` |
| Latest release tag | none checked out | `v2026.7.20` | `git tag` |
| `origin/main` HEAD | `d7b36070e` (also pending) | `d7b36070e` | `git fetch origin main` |
| Integration branch HEAD | `cfb9535cd` | — | active soak repo |
| Integration merge-base with main | `7b5ba205` | — | `git merge-base` |
| Fuli repository pin | `727ce926` | `727ce926` | `git rev-parse` |

## 2. Commit/Tag Delta

- `v2026.7.7` → `v2026.7.20` (mainline) includes `0.19.0` release.
- The integration branch is **29 commits ahead** of `origin/main` and **1319 commits behind** the merge-base count from main to integration base. In practical terms, the Fuli work must be rebased/merged onto the new `0.19.0` mainline.
- Recent mainline commits touching the same areas:
  - `d7b36070e` — `fix(checkpoints): honor gateway config and task cwd`
  - `e2fd8a37d` — desktop session-switch repo-status refresh
  - `3ef6bbd20` — `chore: release v0.19.0`
  - Multiple `fix(honcho)` commits around timeout staleness and client rebuild

## 3. Relevant Changed Files

### Memory / providers
- `plugins/memory/honcho/client.py` — timeout staleness, rebuild on config change.
- `hermes_cli/memory_providers.py` — provider discovery CLI wiring.
- `hermes_cli/plugins.py` — plugin loading.

### Desktop UI
- `apps/desktop/src/app/settings/memory/provider-config-modal.tsx`
- `apps/desktop/src/app/settings/memory/provider-config-panel.tsx`
- `apps/desktop/src/app/settings/provider-config-panel.tsx`
- `apps/desktop/src/app/settings/plugins-settings.tsx`
- `apps/desktop/electron/power-save.ts` — keep-awake changes.

### Gateway / CLI
- `gateway/run.py` — checkpoint config / task cwd handling.
- `gateway/platforms/api_server.py` — minor.
- `agent/tool_executor.py` — checkpoint path handling.
- `hermes_cli/main.py`, `hermes_cli/subcommands/dashboard.py` — CLI wiring.

### State / persistence
- `checkpoints` behavior in `gateway/run.py` and `agent/tool_executor.py`.
- No database migrations observed in the diff.

## 4. Breaking-Change Assessment

- **No changes to the memory-provider ABC or plugin.yaml schema** in mainline.
- **Honcho client internals changed**, but the provider surface (`initialize`, `health`, `handle_tool_call`) is unchanged.
- **Provider config panel UI changed** in the desktop app; new Fuli provider entries will need UI registration, but the existing shadow/Fuli panels can follow the same pattern.
- **Checkpoint/gateway path fixes** are additive and do not affect Fuli product behavior.

## 5. Fuli / Shadow Conflict Risk

| Risk | Level | Rationale |
|---|---|---|
| `hermes_cli/memory_providers.py` overlap | Medium | Both branches modify provider listing; need merge review. |
| Desktop provider config panels | Medium | New UI fields need to include Fuli and shadow modes. |
| `gateway/run.py` checkpoint paths | Low | Additive; no direct conflict with Fuli. |
| Honcho client timeout logic | Low | Fuli uses its own client; shadow delegates to Honcho only for primary reads. |
| Config schema | Low | Fuli config is isolated under `memory.fuli_product`. |

## 6. Config Migration Risk

- Current observed default config has `_config_version: 33` and includes all current mainline fields.
- The Fuli product config uses a separate `fuli_product` subkey; it does not collide with existing top-level keys.
- Migration from the pilot config (`memory.shadow.*`) to the product config (`memory.fuli_product.*`) is a one-time transform with a dry-run option.
- Risk: **low**, provided migration is tested on a copy of the user's config in the isolated worktree.

## 7. Rollback Path

1. Keep the `integration/fuli-v0.18.2` branch and the running soak untouched.
2. Do all product work in the isolated worktree `product/fuli-memory-v1`.
3. If the merged product branch fails tests, revert the worktree to `origin/main` and re-apply the 29 Fuli commits cleanly.
4. Never overwrite the live `~/.hermes/config.yaml` or `~/.hermes/profiles/shadow-pilot/config.yaml` from the product worktree.

## 8. Default Config Drift Investigation

- **Prior SHA:** `f3a36572d65bfbc227c3dc943849ef0c486d26d933900430b708d6a5106947b7`
- **Current SHA:** `79b0b0de4e4bf2575ef462d4ba96ff3cbccd30ea49fbdce688f196b470317da9`
- The current file is **schema-valid** (`_config_version: 33`) and contains **no Fuli or shadow settings**; `memory.provider` remains `honcho`.
- New fields observed relative to the old SHA include: `display.pet`, `memory.memory_enabled`, `memory.user_profile_enabled`, `delegation.*`, `moa.*`, `skills.*`, `curator.*`, `model_catalog.*`, `computer_use.*`, `x_search.*`, `platform_toolsets.*`, `mcp_servers.*`, `cron_mode`, `rich_messages`, and the `fallback_model` comment block.
- Likely cause: the Hermes dashboard (PID 521, started before the soak) or a `hermes` CLI invocation auto-upgraded the config to the current schema version. No secrets were added; existing secrets are unchanged.
- **No action required.** The drift is benign and unrelated to the Fuli product.

## 9. Recommendation

**Safe with adaptation.**

The update is safe to proceed with once:
1. The 29 Fuli integration commits are rebased onto `origin/main` (`d7b36070e`) in the isolated worktree.
2. Conflicts in `hermes_cli/memory_providers.py` and the desktop provider-config panels are resolved.
3. The full product test suite (Phase 7 compatibility matrix) passes.
4. The running soak is allowed to complete and is tagged before any live deployment.

No update should be applied to the active runtime while PID 44580 is running.
