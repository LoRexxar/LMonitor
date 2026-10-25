#!/bin/bash
# Planned rotation is cooperative and owned by the backend's admission gate.
# INT/TERM/HUP to this supervisor are explicit operator stops, NOT rotation.
set -u
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESTART_INTERVAL="${RESTART_INTERVAL:-21600}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"
STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-3}"
BACKEND_LOCK_FILE="${BACKEND_LOCK_FILE:-/tmp/lmonitor-backend-$(id -u).lock}"

for value in "$RESTART_INTERVAL" "$CHECK_INTERVAL" "$STOP_GRACE_SECONDS"; do
    [[ "$value" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] || {
        echo 'backend intervals must be finite nonnegative numbers' >&2; exit 2;
    }
done
[[ "$CHECK_INTERVAL" =~ [1-9] && "$STOP_GRACE_SECONDS" =~ [1-9] ]] || {
    echo 'check interval and stop grace must be positive' >&2; exit 2;
}

# Refuse a second supervisor; never discover/kill unrelated workers by argv.
exec 9>"$BACKEND_LOCK_FILE"
flock -n 9 || { echo 'backend supervisor already running' >&2; exit 1; }

if [ "$#" -eq 0 ]; then
    PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
    set -- "$PYTHON_BIN" "$PROJECT_DIR/manage.py" LMonitorCoreBackend
fi

child=''
stop_child() {
    trap '' INT TERM HUP
    if [ -n "$child" ]; then
        # The child owns this process group (including browser/helper children).
        kill -TERM -- "-$child" 2>/dev/null || true
        sleep "$STOP_GRACE_SECONDS"
        kill -KILL -- "-$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
trap stop_child INT TERM HUP

export LMONITOR_BACKEND_RETIRE_AFTER="$RESTART_INTERVAL"
cd "$PROJECT_DIR" || exit 1
while :; do
    echo 'start backend'
    # Inherit the lock so a supervisor crash cannot start a competing child.
    # Self-retirement starts after lifecycle setup; no startup signal race.
    setsid "$@" &
    child=$!
    wait "$child"
    status=$?
    # Reap leftover helpers from a crash/abnormal drain before starting anew.
    kill -TERM -- "-$child" 2>/dev/null || true
    kill -KILL -- "-$child" 2>/dev/null || true
    child=''
    echo "backend exited status=$status; restarting"
    sleep "$CHECK_INTERVAL"
done
