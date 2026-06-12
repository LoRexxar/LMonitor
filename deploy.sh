#!/bin/bash
set -e

echo "=== 1. Git pull ==="
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Collectstatic ==="
python3 manage.py collectstatic --no-input

echo "=== 3. 重启 lmweb ==="
screen -S lmweb -X stuff $'\cc'
sleep 2
screen -S lmweb -X stuff $'python3 manage.py runserver 0.0.0.0:18000\n'

echo "=== 4. 重启 lmback ==="
screen -S lmback -X stuff $'\cc'
sleep 2
screen -S lmback -X stuff $'./start.sh\n'

echo "=== 部署完成 ==="
