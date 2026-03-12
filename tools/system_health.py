#!/usr/bin/env python3
# description: comprehensive system health report including CPU, memory, disk, uptime and temperature
import os
import subprocess
import time

def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception as e:
        return f"(error: {e})"

print("=== System Health Report ===")
print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
print()

print("--- Uptime ---")
print(run("uptime"))
print()

print("--- Memory ---")
print(run("free -h"))
print()

print("--- Disk ---")
print(run("df -h --exclude-type=tmpfs --exclude-type=devtmpfs 2>/dev/null || df -h"))
print()

print("--- Temperature ---")
try:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        temp_mc = int(f.read().strip())
    print(f"CPU: {temp_mc/1000:.1f}C")
except Exception:
    print("Temperature sensor not available.")
print()

print("--- Top 5 Processes by CPU ---")
print(run("ps aux --sort=-%cpu | awk 'NR>1 && NR<=6 {printf \"%-20s %5s%% CPU %5s%% MEM\\n\", $11, $3, $4}'"))
