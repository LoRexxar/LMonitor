#!/bin/bash

RESTART_INTERVAL="${RESTART_INTERVAL:-21600}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"

PATTERN="${PATTERN:-LMonitorCoreBackend}"
start_ts=0

kill_existing() {
    pids="$(ps aux | grep "$PATTERN" | grep -v grep | awk '{print $2}')"
    if [ -n "$pids" ]; then
        kill -INT $pids 2>/dev/null || true
        sleep 3
        pids2="$(ps aux | grep "$PATTERN" | grep -v grep | awk '{print $2}')"
        if [ -n "$pids2" ]; then
            kill -KILL $pids2 2>/dev/null || true
        fi
    fi
}

start_child() {
    /usr/bin/python3 /root/manage.py LMonitorCoreBackend &
    start_ts="$(date +%s)"
}

is_running() {
    if [ "$(ps aux | grep "$PATTERN" | grep -v grep | wc -l)" -gt 0 ]; then
        return 0
    fi
    return 1
}

trap 'kill_existing; exit 0' INT TERM

echo "start"
kill_existing
start_child

while :; do
    now="$(date +%s)"

    if ! is_running; then
        echo "start"
        kill_existing
        start_child
    fi

    if [ "$RESTART_INTERVAL" -gt 0 ] && [ "$start_ts" -gt 0 ] && [ $((now - start_ts)) -ge "$RESTART_INTERVAL" ]; then
        echo "restart"
        kill_existing
        start_child
    fi

    sleep "$CHECK_INTERVAL"
done
