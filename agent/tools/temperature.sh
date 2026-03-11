#!/bin/bash
# tool: temperature
# description: read CPU temperature
if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
  temp=$(cat /sys/class/thermal/thermal_zone0/temp)
  echo "CPU temp (milliC): $temp"
else
  echo "Temperature sensor not available"
fi

