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
MANAGE_SIMC_WORKER="${MANAGE_SIMC_WORKER:-1}"
WOW_BUILD="12.0.7.68453"
if [ -x .venv/bin/python ]; then
    PYTHON_BIN=".venv/bin/python"
fi

echo "=== 1. Git pull ==="
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Migrate ==="

"$PYTHON_BIN" manage.py migrate --no-input

echo "=== 3. 初始化/更新大秘境规划器数据 ==="
if [ "${IMPORT_MDT_DATA:-0}" = "1" ]; then
    "$PYTHON_BIN" manage.py import_mythic_dungeon_data --activate --replace
elif ! "$PYTHON_BIN" manage.py shell -c \
    "from botend.models import MythicDungeonDataVersion; raise SystemExit(0 if MythicDungeonDataVersion.objects.filter(is_active=True).exists() else 1)"
then
    "$PYTHON_BIN" manage.py import_mythic_dungeon_data --activate
else
    echo "已存在活动的大秘境数据版本，跳过导入。"
fi

echo "=== 4. Collectstatic ==="
"$PYTHON_BIN" manage.py collectstatic --no-input

echo "=== 5. 应用 SimC 本地补丁 ==="
if [ "$MANAGE_SIMC_WORKER" = "1" ]; then
    "$PYTHON_BIN" manage.py update_simc_binary --apply-patches --threads 2 --wow-build "$WOW_BUILD"
else
    echo "SimC Worker 由独立部署管理，跳过二进制更新。"
fi

echo "=== 6. 重启 lmweb ==="
screen -S lmweb -X quit 2>/dev/null || true
kill_processes 'manage.py runserver 0.0.0.0:18000'
sleep 2
screen -dmS lmweb bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py runserver 0.0.0.0:18000"

echo "=== 7. 重启 lmback ==="
screen -S lmback -X quit 2>/dev/null || true
kill_processes 'LMonitorCoreBackend'
sleep 2
screen -dmS lmback bash -lc 'cd ~/LMonitor && ./start.sh'

echo "=== 8. 重启 lmsimc ==="
if [ "$MANAGE_SIMC_WORKER" = "1" ]; then
    screen -S lmsimc -X quit 2>/dev/null || true
    kill_processes 'simc_worker.py|manage.py simc_worker'
    sleep 2
    screen -dmS lmsimc bash -lc 'cd ~/LMonitor && exec ./start_simc_worker.sh'
else
    echo "SimC Worker 由独立部署管理，跳过重启。"
fi

echo "=== 9. 检查 screen 会话 ==="
if [ "$MANAGE_SIMC_WORKER" = "1" ]; then
    screen -list | grep -E '\.(lmweb|lmback|lmsimc)'
else
    screen -list | grep -E '\.(lmweb|lmback)'
fi

echo "=== 部署完成 ==="
