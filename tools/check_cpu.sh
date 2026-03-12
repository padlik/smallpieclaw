#!/bin/bash
# description: show CPU usage percentage and load averages
echo "=== CPU Usage ==="
top -bn1 | grep "Cpu(s)" | awk -F',' '{print $4}' | awk '{print "CPU idle: " $1 "%"}'
echo ""
echo "=== Load Averages ==="
uptime
