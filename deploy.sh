#!/bin/bash
set -e

echo "=== 1. Git pull ==="
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Collectstatic ==="
python3 manage.py collectstatic --no-input

echo "=== 3. 重启 lmweb ==="
if screen -list | grep -q '[.]lmweb[[:space:]]'; then
    screen -S lmweb -X stuff $'\cc'
    sleep 2
    screen -S lmweb -X stuff $'python3 manage.py runserver 0.0.0.0:18000\n'
else
    screen -dmS lmweb bash -lc 'cd ~/LMonitor && python3 manage.py runserver 0.0.0.0:18000'
fi

echo "=== 4. 重启 lmback ==="
if screen -list | grep -q '[.]lmback[[:space:]]'; then
    screen -S lmback -X stuff $'\cc'
    sleep 2
    screen -S lmback -X stuff $'./start.sh\n'
else
    screen -dmS lmback bash -lc 'cd ~/LMonitor && ./start.sh'
fi

echo "=== 部署完成 ==="
