#!/bin/bash

kill -2 $(ps aux | grep LMonitorCoreBackend|grep -v grep|awk '{print $2}')
sleep 3
kill -9 $(ps aux | grep LMonitorCoreBackend|grep -v grep|awk '{print $2}')