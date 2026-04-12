#!/bin/bash
# description: show CPU and GPU temperature sensors on Raspberry Pi
echo "=== System Temperature ==="
if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
  cpu_temp=$(cat /sys/class/thermal/thermal_zone0/temp)
  echo "CPU Temperature: $((cpu_temp/1000)).$((cpu_temp%1000/100))C"
else
  echo "CPU temp sensor not found."
fi
if command -v vcgencmd &>/dev/null; then
  echo "GPU: $(vcgencmd measure_temp 2>/dev/null)"
fi
