# Phase 5 current-main compatibility matrix

- worktree: `/Users/caiado/.hermes/worktrees/hermes-fuli-compat`
- head_sha: `78c06525e8e955e06a007b07b347c679f3977c3e`
- head_short: `78c06525e`
- tag: `v2026.7.20-1277-g78c06525e`
- python_version: `3.11.14`
- product_commit: `29bc8af0b0f1de9a35cf7fa66581fc9df4750faf`
- qualified_commit: `cfb9535cd38243988d0298771350c048885cf375`

| ID | label | boot | provider | config | migration | rollback | persistence | cli | pass/fail |
|----|-------|------|----------|--------|-----------|----------|-------------|-----|-----------|
| A | origin/main (no Fuli product, no qualified runtime) | ok | n/a | ok | n/a | n/a | n/a | ok | PASS |
| B | origin/main + qualified runtime (pilot/, shadow plugin) | ok | n/a | ok | n/a | n/a | n/a | ok | PASS |
| C | origin/main + qualified runtime + product | ok | n/a | ok | n/a | n/a | n/a | ok | PASS |
| D | Fuli package unavailable | ok | False | ok | n/a | n/a | n/a | ok | PASS |
| E | legacy shadow config migration | ok | n/a | ok | shadow | n/a | n/a | ok | PASS |
| F | rollback to Honcho-only | ok | n/a | ok | n/a | honcho | n/a | ok | PASS |
| G | corrupt comparison DB | ok | n/a | ok | n/a | n/a | 0 | ok | PASS |
| H | restart after auto-pause | ok | n/a | ok | n/a | n/a | n/a | ok | PASS |

**Summary:** 8/8 scenarios pass.

