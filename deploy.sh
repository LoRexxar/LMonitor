#!/bin/bash
set -e

exec 9>/tmp/lmonitor-deploy.lock
flock -n 9 || { echo "另一个部署正在运行"; exit 1; }

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
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Migrate ==="

"$PYTHON_BIN" manage.py migrate --no-input

echo "=== 3. Collectstatic ==="
"$PYTHON_BIN" manage.py collectstatic --no-input --ignore='simc_results/*'

echo "=== 4. 更新天赋模拟器数据 ==="
TALENT_BUILD="12.1.0.69283"
TALENT_DUMP_DIR=".cache/wago_db2_dumps/${TALENT_BUILD}"
rm -rf "$TALENT_DUMP_DIR"
mkdir -p "$TALENT_DUMP_DIR"
tar -xzf "botend/data/ptr_talent_db2_${TALENT_BUILD}.tar.gz" -C "$TALENT_DUMP_DIR"
"$PYTHON_BIN" manage.py repair_ptr_talent_metadata \
    --version-key ptr-12.1.0 \
    --dump-dir "$TALENT_DUMP_DIR" \
    --backup-dir .cache/backups \
    --skip-wowhead

echo "=== 5. 重启 lmweb ==="
screen -S lmweb -X quit 2>/dev/null || true
kill_processes 'manage.py runserver 0.0.0.0:18000'
sleep 2
screen -dmS lmweb bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py runserver 0.0.0.0:18000 --noreload"

echo "=== 6. 重启 lmback ==="
screen -S lmback -X quit 2>/dev/null || true
kill_processes 'LMonitorCoreBackend'
sleep 2
screen -dmS lmback bash -lc 'cd ~/LMonitor && ./start.sh'

echo "=== 7. 重启 lmsimc ==="
screen -S lmsimc -X quit 2>/dev/null || true
kill_processes 'manage.py simc_worker'
sleep 2
screen -dmS lmsimc bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py simc_worker"

"$PYTHON_BIN" manage.py recover_interrupted_simc_update

echo "=== 8. 检查服务状态 ==="
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
