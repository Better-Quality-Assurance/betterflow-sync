#!/usr/bin/env bash
# Install dist/BetterFlow.app into /Applications, terminating any running
# instance first and waiting for it to shut down cleanly before replacing
# the bundle.
#
# Usage:
#   ./scripts/install-mac.sh                 # install from dist/BetterFlow.app
#   ./scripts/install-mac.sh path/to.app     # install from a custom path
#
# Why this exists: doing `killall BetterFlow; cp -R dist/BetterFlow.app /Applications/`
# races the running process. The app's shutdown does real work (offline queue
# flush, SQLite close, launchd unbootstrap; see commit 19ec642), so cp can
# land on top of files the old process is still holding.

set -euo pipefail

SRC_APP="${1:-dist/BetterFlow.app}"
DEST_APP="/Applications/BetterFlow.app"
APP_PROCESS="BetterFlow"

# Grace period for the app to finish its shutdown work. The app flushes the
# offline queue, closes SQLite, and may unbootstrap autostart on the way out.
# Don't lower this without testing on a busy queue.
SHUTDOWN_GRACE=20

if [[ ! -d "$SRC_APP" ]]; then
    echo "[install-mac] Source app not found: $SRC_APP" >&2
    echo "[install-mac] Run 'make build-mac' first." >&2
    exit 1
fi

stop_running_app() {
    local pids
    pids=$(pgrep -x "$APP_PROCESS" || true)
    if [[ -z "$pids" ]]; then
        echo "[install-mac] No running $APP_PROCESS process."
        return 0
    fi

    echo "[install-mac] Sending SIGTERM to $APP_PROCESS (pids: $pids)"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true

    local waited=0
    while (( waited < SHUTDOWN_GRACE )); do
        if ! pgrep -x "$APP_PROCESS" >/dev/null; then
            echo "[install-mac] $APP_PROCESS exited after ${waited}s."
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    pids=$(pgrep -x "$APP_PROCESS" || true)
    if [[ -n "$pids" ]]; then
        echo "[install-mac] $APP_PROCESS still running after ${SHUTDOWN_GRACE}s, sending SIGKILL (pids: $pids)" >&2
        # shellcheck disable=SC2086
        kill -KILL $pids 2>/dev/null || true
        sleep 1
    fi
}

stop_running_app

if [[ -d "$DEST_APP" ]]; then
    echo "[install-mac] Removing existing $DEST_APP"
    rm -rf "$DEST_APP"
fi

echo "[install-mac] Copying $SRC_APP -> $DEST_APP"
# ditto preserves resource forks, ACLs, and extended attributes; cp -R can
# lose extended attrs that codesign/Gatekeeper rely on.
ditto "$SRC_APP" "$DEST_APP"

# Clear the quarantine attribute that Gatekeeper otherwise applies to freshly
# copied builds. Unsigned dev builds refuse to launch with it set.
xattr -dr com.apple.quarantine "$DEST_APP" 2>/dev/null || true

echo "[install-mac] Installed: $DEST_APP"
echo "[install-mac] Launch with: open '$DEST_APP'"
