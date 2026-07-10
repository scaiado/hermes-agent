#!/usr/bin/env bash
# Run a Hermes instance in an isolated sandbox — separate HERMES_HOME,
# separate Electron userData, and a distinct Desktop app name so it doesn't compete
# with your main desktop instance's single-instance lock.
#
# By default the sandbox is throwaway: a temp dir is created and removed on
# exit. Use --persistent to keep the sandbox across restarts (stored under
# .hermes-sandbox/ in the worktree git root).
#
# Usage:
#   scripts/dev-sandbox.sh python -m hermes_cli.main
#   scripts/dev-sandbox.sh hermes desktop
#   scripts/dev-sandbox.sh electron .
#   scripts/dev-sandbox.sh -- npm run dev   # from apps/desktop/
#   scripts/dev-sandbox.sh --persistent hermes desktop
#   scripts/dev-sandbox.sh --persistent -- npm run dev
#
# Override the app name (default: HermesSandbox):
#   HERMES_DEV_SANDBOX_NAME=Staging scripts/dev-sandbox.sh hermes desktop
#
# Override the persistent sandbox dir name (default: .hermes-sandbox):
#   HERMES_DEV_SANDBOX_DIR=.staging-sandbox scripts/dev-sandbox.sh --persistent hermes desktop

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_help() {
  cat <<'EOF'
Usage: dev-sandbox.sh [--persistent] [--] <command...>

Run a Hermes instance in an isolated sandbox.

Options:
  --persistent    Keep the sandbox dir across restarts (under the worktree
                  git root, in .hermes-sandbox/). Without this flag the
                  sandbox is a temp dir that is removed on exit.
  -h, --help      Show this help message.

Environment:
  HERMES_DEV_SANDBOX_NAME  Override the app name (default: HermesSandbox)
  HERMES_DEV_SANDBOX_DIR   Override the persistent dir name (default: .hermes-sandbox)

Examples:
  dev-sandbox.sh hermes desktop
  dev-sandbox.sh --persistent hermes desktop
  dev-sandbox.sh -- npm run dev
EOF
}

PERSISTENT=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --persistent)
      PERSISTENT=true
      shift
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

if [ "$#" -eq 0 ]; then
  print_help >&2
  exit 1
fi

SANDBOX_NAME="${HERMES_DEV_SANDBOX_NAME:-HermesSandbox}"

if [ "$PERSISTENT" = true ]; then
  SANDBOX_DIR_NAME="${HERMES_DEV_SANDBOX_DIR:-.hermes-sandbox}"
  GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR/..")"
  SANDBOX_ROOT="$(cd "$GIT_ROOT" && pwd)/$SANDBOX_DIR_NAME"
else
  SANDBOX_ROOT="$(mktemp -d -t hermes-sandbox.XXXXXX)"
fi

export HERMES_HOME="$SANDBOX_ROOT/hermes-home"
export HERMES_DESKTOP_USER_DATA_DIR="$SANDBOX_ROOT/user-data"
export HERMES_DESKTOP_APP_NAME="$SANDBOX_NAME"

mkdir -p "$HERMES_HOME" "$HERMES_DESKTOP_USER_DATA_DIR"

echo "[sandbox] HERMES_HOME=$HERMES_HOME" >&2
echo "[sandbox] userData=$HERMES_DESKTOP_USER_DATA_DIR" >&2
echo "[sandbox] appName=$HERMES_DESKTOP_APP_NAME" >&2
if [ "$PERSISTENT" = true ]; then
  echo "[sandbox] persistent: $SANDBOX_ROOT" >&2
else
  echo "[sandbox] ephemeral (will be cleaned up on exit)" >&2
fi

if [ "$PERSISTENT" = false ]; then
  cleanup() {
    rm -rf -- "$SANDBOX_ROOT"
  }
  trap cleanup EXIT
  trap 'cleanup; exit 130' INT TERM
fi

"$@"
rc=$?
exit $rc
