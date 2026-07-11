# Fuli/Shadow Memory Integration — Controlled Mac mini Cutover Plan

## Current state
- Integration branch: `integration/fuli-latest-upstream` on `scaiado/hermes-agent`
- Based on `origin/main` at `4281151ae`
- Replays Fuli + shadow memory providers from `feat/fuli-shadow-memory`
- Aligned to Fuli-Memory-Core P0 contract (`feat/p0-reliability`, commit `727ce926`)
- Validation: focused tests pass, 0.01h pilot passes all gates

## Rollback artifacts (created before this work)
- `~/.hermes/profiles/shadow-pilot` — backed up and excluded from git
- Live Hermes profile / state — captured separately (see earlier backup bundle)
- Source rollback branch: `feat/fuli-shadow-memory` is unchanged

## Cutover steps
1. **Stop any running Hermes gateway / cron / pilot processes.**
2. **Switch the managed hermes-agent checkout to the integration branch:**
   ```bash
   cd ~/.hermes/hermes-agent
   git fetch scaiado
   git switch -C integration/fuli-latest-upstream scaiado/integration/fuli-latest-upstream
   git log --oneline -5
   ```
3. **Verify venv dependencies are still compatible** (no new package changes in this branch):
   ```bash
   .venv/bin/python -m pytest tests/plugins/test_fuli_provider.py tests/plugins/test_shadow_provider.py -q
   ```
4. **If using the shadow-pilot profile, sync its config to a fresh copy from backup**:
   ```bash
   cp -R ~/.hermes/backups/fuli-shadow-YYYY-MM-DD-HHMMSS/profiles/shadow-pilot ~/.hermes/profiles/
   ```
5. **Restart gateway / cron / pilot.**
6. **Monitor first real session end** to confirm Fuli `on_session_end` flush and no Honcho writes lost.

## Rollback steps
1. Stop Hermes processes.
2. Switch back to previous branch (likely `feat/fuli-shadow-memory` or `main`):
   ```bash
   cd ~/.hermes/hermes-agent
   git switch feat/fuli-shadow-memory  # or main
   ```
3. Restore the shadow-pilot profile from backup if it was modified.
4. Restart processes.

## Validation gates after cutover
- `hermes shadow preflight` returns OK
- A 0.01h shadow pilot run passes all pass gates
- A real conversation persists memories (Honcho primary) and mirrors them to Fuli
- `hermes memory status` shows both providers healthy

## Risks and mitigations
| Risk | Mitigation |
|------|-----------|
| New upstream API drift | Integration branch is pinned to `origin/main` at `4281151ae`; future upstream syncs require re-replay. |
| P0 contract changes | `docs/ops/fuli-provider-pin.md` records the approved Fuli-Memory-Core commit. |
| Shadow pilot data pollution | Runs in dedicated `shadow-pilot` profile; `hermes shadow cleanup` can remove synthetic rows. |
| Performance regression on session end | `on_session_end` has bounded timeout; fallback to legacy `add()` if `add_with_status` unavailable. |
