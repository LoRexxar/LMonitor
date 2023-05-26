#!/bin/bash

while :
do
    if [ $(ps aux | grep LBotCoreBackend|grep -v grep|wc -l) -eq 0 ];then
        echo "start"
        /usr/bin/python3 /home/ubuntu/LMonitor/manage.py LBotCoreBackend
    fi
    sleep 100
done
