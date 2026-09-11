#!/bin/bash
set -e

DEPLOY_SCRIPT="$(readlink -f "$0")"
PROJECT_DIR="$(dirname "$DEPLOY_SCRIPT")"
DEPLOY_LOCK_FILE="${DEPLOY_LOCK_FILE:-/tmp/lmonitor-deploy.lock}"
cd "$PROJECT_DIR"

if [ "${LMONITOR_DEPLOY_REEXEC:-0}" = "1" ]; then
    { : >&9; } 2>/dev/null || {
        echo "部署脚本自更新后未继承部署锁"
        exit 1
    }
else
    exec 9>"$DEPLOY_LOCK_FILE"
    flock -n 9 || { echo "另一个部署正在运行"; exit 1; }
fi

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

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x .venv/bin/python ]; then
    PYTHON_BIN=".venv/bin/python"
fi

echo "=== 1. Git pull ==="
DEPLOY_HASH_BEFORE="$(sha256sum "$DEPLOY_SCRIPT" | cut -d' ' -f1)"
GIT_MERGE_AUTOEDIT=no git pull origin master
DEPLOY_HASH_AFTER="$(sha256sum "$DEPLOY_SCRIPT" | cut -d' ' -f1)"

if [ "$DEPLOY_HASH_BEFORE" != "$DEPLOY_HASH_AFTER" ]; then
    if [ "${LMONITOR_DEPLOY_REEXEC:-0}" = "1" ]; then
        echo "部署脚本在自更新重启后再次变化，停止以避免循环"
        exit 1
    fi
    echo "deploy.sh 已更新，保持部署锁并切换到新脚本"
    export LMONITOR_DEPLOY_REEXEC=1
    exec "$DEPLOY_SCRIPT" "$@"
fi

echo "=== 2. Migrate ==="

"$PYTHON_BIN" manage.py migrate --no-input

echo "=== 3. Collectstatic ==="
"$PYTHON_BIN" manage.py collectstatic --no-input --ignore='simc_results/*'

echo "=== 4. 重启 lmweb ==="
screen -S lmweb -X quit 2>/dev/null || true
kill_processes 'manage.py runserver 0.0.0.0:18000'
sleep 2
screen -dmS lmweb bash -lc "cd '$PROJECT_DIR' && $PYTHON_BIN manage.py runserver 0.0.0.0:18000 --noreload"

echo "=== 5. 重启 lmback ==="
screen -S lmback -X quit 2>/dev/null || true
kill_processes 'LMonitorCoreBackend'
sleep 2
screen -dmS lmback bash -lc "cd '$PROJECT_DIR' && ./start.sh"

echo "=== 6. 重启 lmsimc ==="
screen -S lmsimc -X quit 2>/dev/null || true
kill_processes 'manage.py simc_worker'
sleep 2
screen -dmS lmsimc bash -lc "cd '$PROJECT_DIR' && $PYTHON_BIN manage.py simc_worker"

"$PYTHON_BIN" manage.py recover_interrupted_simc_update

echo "=== 7. 检查服务状态 ==="
for session in lmweb lmback lmsimc; do
    screen -list | grep -q "\.${session}" || {
        echo "screen 会话 ${session} 启动失败"
        exit 1
    }
done

web_ready=0
for _ in $(seq 1 15); do
    if curl -fsS http://127.0.0.1:18000/ >/dev/null; then
        web_ready=1
        break
    fi
    sleep 1
done
[ "$web_ready" = "1" ] || { echo "lmweb HTTP 健康检查失败"; exit 1; }
screen -list | grep -E '\.(lmweb|lmback|lmsimc)'

echo "=== 部署完成 ==="
