#!/bin/bash
set -e

cd /home/lighthouse

echo "=== 1. Git pull ==="
git pull

echo "=== 2. Git merge ==="
# 自动接受默认 merge message，不打开 vim
GIT_MERGE_AUTOEDIT=no git merge origin/master

echo "=== 3. Collectstatic ==="
# 自动回答 yes 覆盖
yes | python3 manage.py collectstatic

echo "=== 4. 重启 lmweb ==="
# 发 Ctrl+C 杀掉旧进程，再启动
screen -S lmweb -X stuff $'\cc'
sleep 2
screen -S lmweb -X stuff $'python3 manage.py runserver 0.0.0.0:1800\n'

echo "=== 5. 重启 lmback ==="
screen -S lmback -X stuff $'\cc'
sleep 2
screen -S lmback -X stuff $'./start.sh\n'

echo "=== 部署完成 ==="
