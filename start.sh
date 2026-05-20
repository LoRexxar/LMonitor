#!/bin/bash

RESTART_INTERVAL="${RESTART_INTERVAL:-21600}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"

child_pid=""
start_ts=0

start_child() {
    /usr/bin/python3 /root/manage.py LMonitorCoreBackend &
    child_pid="$!"
    start_ts="$(date +%s)"
}

stop_child() {
    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        kill -INT "$child_pid" 2>/dev/null || true
        for ((i=0; i<30; i++)); do
            sleep 1
            if ! kill -0 "$child_pid" 2>/dev/null; then
                break
            fi
        done
        if kill -0 "$child_pid" 2>/dev/null; then
            kill -KILL "$child_pid" 2>/dev/null || true
        fi
    fi
    child_pid=""
}

trap 'stop_child; exit 0' INT TERM

echo "start"
start_child

while :; do
    now="$(date +%s)"

    if [ -n "$child_pid" ] && ! kill -0 "$child_pid" 2>/dev/null; then
        child_pid=""
    fi

    if [ -z "$child_pid" ]; then
        echo "start"
        start_child
        continue
    fi

    if [ "$RESTART_INTERVAL" -gt 0 ] && [ $((now - start_ts)) -ge "$RESTART_INTERVAL" ]; then
        echo "restart"
        stop_child
        echo "start"
        start_child
    fi

    sleep "$CHECK_INTERVAL"
done
