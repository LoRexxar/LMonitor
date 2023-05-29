<<<<<<< HEAD
#!/bin/bash

while :
do
    if [ $(ps aux | grep LMonitorCoreBackend|grep -v grep|wc -l) -eq 0 ];then
        echo "start"
        /usr/bin/python3 /home/ubuntu/LMonitor/manage.py LMonitorCoreBackend
    fi
    sleep 100
done
=======
#!/bin/bash


while :
do
    if [ $(ps aux | grep LMonitorCoreBackend|grep -v grep|wc -l) -eq 0 ];then
        echo "start"
        /usr/bin/python3 /root/manage.py LMonitorCoreBackend
    fi
    sleep 100
done
>>>>>>> e7cc5a8193a073e6a0097de29aa07fc742355dd7
