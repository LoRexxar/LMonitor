#!/bin/bash

while :
do
    if [ $(ps aux | grep LMonitorCoreBackend|grep -v grep|wc -l) -eq 0 ];then
        echo "start"
        /usr/bin/python3 /home/ubuntu/LMonitor/manage.py LMonitorCoreBackend
    fi
    sleep 100
done
