---
name: system-health
description: Comprehensive system health diagnostics — CPU, memory, disk, temperature, and service status reporting for Linux/Raspberry Pi.
license: MIT
compatibility: Linux
---

# System Health Skill

Perform a full system health check and produce a concise human-readable report.

## What to check

1. **CPU** — current load (1/5/15 min), core count, frequency if available
2. **Memory** — total, used, free, swap usage
3. **Disk** — usage for `/` and any mounted volumes above 70% threshold
4. **Temperature** — CPU temperature (`/sys/class/thermal/thermal_zone0/temp` on Pi)
5. **Uptime** — system uptime and last boot time
6. **Services** — status of critical systemd services (e.g., `ssh`, `docker` if present)

## Tools to use

- `shell` — run system commands (`free -h`, `df -h`, `uptime`, `top -bn1`, `systemctl is-active <service>`)
- `file_read` — read `/proc/loadavg`, `/sys/class/thermal/thermal_zone0/temp`, etc.

## Report format

Return a single Telegram message using this structure:

```
🖥 System Health Report
━━━━━━━━━━━━━━━━━━━━
🔥 CPU Load:   0.45 / 0.32 / 0.28  (4 cores)
🌡 CPU Temp:   48.3 °C
🧠 Memory:     512 MB / 2.0 GB used (25%)
💾 Disk (/):   8.1 GB / 32 GB used (25%)
⏱ Uptime:     3 days, 14:22:05
🔌 Services:   ssh ✅  docker ✅
```

- Use ⚠️ for values above warning threshold (e.g., temp > 70°C, disk > 85%)
- Use ❌ for services that are not active

## Notes

- On non-Pi Linux, skip temperature if thermal path is unavailable
- Keep the message under 500 characters for Telegram
