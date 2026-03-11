#!/usr/bin/env python3
# description: comprehensive system health check returning a structured summary

import subprocess
import platform
import os
import json
from pathlib import Path


def run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception as e:
        return f"[error: {e}]"


def disk_info() -> dict:
    lines = run(["df", "-h", "/"]).splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        return {
            "total": parts[1] if len(parts) > 1 else "?",
            "used": parts[2] if len(parts) > 2 else "?",
            "available": parts[3] if len(parts) > 3 else "?",
            "percent": parts[4] if len(parts) > 4 else "?",
        }
    return {}


def memory_info() -> dict:
    lines = run(["free", "-h"]).splitlines()
    for line in lines:
        if line.startswith("Mem:"):
            parts = line.split()
            return {
                "total": parts[1] if len(parts) > 1 else "?",
                "used": parts[2] if len(parts) > 2 else "?",
                "available": parts[6] if len(parts) > 6 else "?",
            }
    return {}


def cpu_temp() -> str:
    temp_file = Path("/sys/class/thermal/thermal_zone0/temp")
    if temp_file.exists():
        try:
            return f"{int(temp_file.read_text().strip()) / 1000:.1f}°C"
        except Exception:
            pass
    return "N/A"


def load_avg() -> str:
    try:
        return ", ".join(f"{x:.2f}" for x in os.getloadavg())
    except Exception:
        return "N/A"


health = {
    "platform": platform.machine(),
    "os": platform.version()[:60],
    "uptime": run(["uptime", "-p"]),
    "load_avg_1_5_15": load_avg(),
    "cpu_temperature": cpu_temp(),
    "disk_root": disk_info(),
    "memory": memory_info(),
}

# Determine overall status
warnings = []
disk_pct = health["disk_root"].get("percent", "0%").rstrip("%")
try:
    if int(disk_pct) > 85:
        warnings.append(f"Disk usage high: {disk_pct}%")
except ValueError:
    pass

temp_str = health["cpu_temperature"].replace("°C", "")
try:
    if float(temp_str) > 70:
        warnings.append(f"CPU temperature high: {health['cpu_temperature']}")
except ValueError:
    pass

health["warnings"] = warnings
health["status"] = "⚠️ Issues detected" if warnings else "✅ All OK"

print(json.dumps(health, indent=2))
