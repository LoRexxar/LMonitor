#!/bin/bash
set -e

kill_processes() {
    local pattern="$1"
    local pids
    pids="$(pgrep -f "$pattern" || true)"
    if [ -n "$pids" ]; then
        kill -INT $pids 2>/dev/null || true
        sleep 3
        pids="$(pgrep -f "$pattern" || true)"
        if [ -n "$pids" ]; then
            kill -KILL $pids 2>/dev/null || true
        fi
    fi
}

echo "=== 1. Git pull ==="
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Migrate ==="
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x .venv/bin/python ]; then
    PYTHON_BIN=".venv/bin/python"
fi
"$PYTHON_BIN" manage.py migrate --no-input

echo "=== 3. Collectstatic ==="
"$PYTHON_BIN" manage.py collectstatic --no-input

echo "=== 4. 重启 lmweb ==="
screen -S lmweb -X quit 2>/dev/null || true
kill_processes 'manage.py runserver 0.0.0.0:18000'
sleep 2
screen -dmS lmweb bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py runserver 0.0.0.0:18000"

echo "=== 5. 重启 lmback ==="
screen -S lmback -X quit 2>/dev/null || true
kill_processes 'LMonitorCoreBackend'
sleep 2
screen -dmS lmback bash -lc 'cd ~/LMonitor && ./start.sh'

echo "=== 6. 重启 lmsimc ==="
screen -S lmsimc -X quit 2>/dev/null || true
kill_processes 'manage.py simc_worker'
sleep 2
screen -dmS lmsimc bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py simc_worker"

echo "=== 7. 检查 screen 会话 ==="
screen -list | grep -E '\.(lmweb|lmback|lmsimc)'

echo "=== 部署完成 ==="
