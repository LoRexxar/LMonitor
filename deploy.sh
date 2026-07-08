#!/bin/bash
set -e

echo "=== 1. Git pull ==="
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Collectstatic ==="
python3 manage.py collectstatic --no-input

echo "=== 3. 重启 lmweb ==="
screen -S lmweb -X quit 2>/dev/null || true
sleep 2
screen -dmS lmweb bash -lc 'cd ~/LMonitor && python3 manage.py runserver 0.0.0.0:18000'

echo "=== 4. 重启 lmback ==="
screen -S lmback -X quit 2>/dev/null || true
sleep 2
screen -dmS lmback bash -lc 'cd ~/LMonitor && ./start.sh'

echo "=== 部署完成 ==="
