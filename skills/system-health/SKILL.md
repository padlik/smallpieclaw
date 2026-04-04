---
name: system-health
description: >
  Comprehensive system health diagnostics for Linux/Raspberry Pi. Use this skill whenever
  the user asks to check system health, run diagnostics, inspect server status, audit
  system security, check for updates, analyze logs, verify firewall rules, monitor
  resources, or troubleshoot performance issues. Trigger even on casual requests like
  "how's my server doing?", "anything wrong with the system?", or "run a full health check".
license: MIT
compatibility: Linux (Debian/Ubuntu/Raspberry Pi OS; adapts gracefully on other distros)
---

# System Health Skill

Perform a full system health check and produce a concise, human-readable report.
Cover all sections below. Skip any section gracefully if the required tool or path is
unavailable (e.g., no `ufw` on the system → note "firewall: not detected").

---

## 1. CPU

```bash
nproc
grep "cpu MHz" /proc/cpuinfo | head -4
cat /proc/loadavg
top -bn1 | grep "Cpu(s)"
```

Report: load averages (1/5/15 min), core count, frequency, % user/system/idle.

---

## 2. Memory & Swap

```bash
free -h
cat /proc/meminfo | grep -E "MemTotal|MemAvailable|SwapTotal|SwapFree"
```

Report: total / used / free RAM, swap usage. Warn ⚠️ if RAM > 85% or swap > 50%.

---

## 3. Disk

```bash
df -h --output=target,size,used,avail,pcent | sort -k5 -rn
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
```

Report all mounted filesystems. Warn ⚠️ at > 70%, alert ❌ at > 90%.

---

## 4. Temperature

```bash
# Raspberry Pi / most ARM SBCs
cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null

# x86 via lm-sensors (if installed)
sensors 2>/dev/null || true

# Alternative via vcgencmd on Pi
vcgencmd measure_temp 2>/dev/null || true
```

Convert raw value (millidegrees) → °C.  
Warn ⚠️ > 70 °C, alert ❌ > 85 °C. Skip silently if no thermal source found.

---

## 5. Uptime & Boot

```bash
uptime -p
who -b
last reboot | head -5
```

Report: human-readable uptime, last boot timestamp, recent reboot history.

---

## 6. Services

Check a curated list; skip services that are not installed.

```bash
for svc in ssh sshd docker nginx apache2 postgresql mysql mariadb redis-server fail2ban ufw cron; do
  systemctl is-active "$svc" 2>/dev/null && echo "$svc: active" || echo "$svc: inactive/missing"
done
```

Report: ✅ active, ❌ inactive (and installed), — not installed.

---

## 7. Networking

```bash
# Interfaces and IPs
ip -brief addr show

# Default gateway and routing table
ip route show

# DNS resolver
cat /etc/resolv.conf | grep nameserver

# Open listening ports (requires ss or netstat)
ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null

# Active connections summary
ss -s 2>/dev/null

# Quick external connectivity test
ping -c 2 -W 2 8.8.8.8 2>/dev/null && echo "internet: reachable" || echo "internet: unreachable"
```

Report: each interface with its IP, default gateway, DNS servers, listening ports with
owning process, and internet reachability.

---

## 8. Firewall

```bash
# ufw (Ubuntu/Debian)
ufw status verbose 2>/dev/null

# iptables fallback
iptables -L -n --line-numbers 2>/dev/null | head -60

# nftables fallback
nft list ruleset 2>/dev/null | head -60
```

Report: firewall tool detected, enabled/disabled status, and a compact list of active
rules. Flag ⚠️ if firewall is inactive on a publicly reachable machine (check for public
IP in `ip addr`).

---

## 9. System Updates

```bash
# Debian / Ubuntu / Raspberry Pi OS
apt-get update -qq 2>/dev/null
apt list --upgradable 2>/dev/null | grep -v "^Listing"

# RPM-based (fallback)
yum check-update 2>/dev/null || dnf check-update 2>/dev/null || true
```

Report: number of pending updates, count of security updates if distinguishable.
Flag ⚠️ if > 0 pending, ❌ if > 20 pending or security updates are overdue.

---

## 10. Kernel & OS

```bash
uname -r          # kernel version
uname -m          # architecture
cat /etc/os-release | grep -E "PRETTY_NAME|VERSION"
lsmod | wc -l     # loaded modules count
dmesg -T --level=err,crit,alert,emerg 2>/dev/null | tail -20  # kernel errors
```

Report: OS name/version, kernel version + architecture, loaded modules count, and any
recent kernel-level errors/warnings from the ring buffer.

---

## 11. Log Analysis

Scan the most relevant log sources for anomalies in the last 24 hours.

```bash
# Auth failures (brute-force / intrusion attempts)
grep -c "Failed password\|authentication failure\|Invalid user" \
  /var/log/auth.log 2>/dev/null || \
  journalctl _SYSTEMD_UNIT=sshd.service --since "24h ago" --no-pager 2>/dev/null | \
  grep -c "Failed\|Invalid" || echo "0"

# OOM killer events
grep -i "oom\|out of memory\|killed process" /var/log/syslog 2>/dev/null | tail -5
journalctl -k --since "24h ago" --no-pager 2>/dev/null | grep -i "oom" | tail -5

# Disk I/O errors
grep -i "i/o error\|read error\|write error\|bad sector" /var/log/syslog 2>/dev/null | tail -5

# General system errors in journal
journalctl -p err --since "24h ago" --no-pager 2>/dev/null | tail -20

# Failed systemd units
systemctl --failed --no-legend 2>/dev/null
```

Report:
- Auth failures count in last 24 h → Flag ⚠️ if > 10, ❌ if > 100
- OOM events → ❌ if any found
- Disk errors → ❌ if any found
- Top 5 most recent journal errors
- Failed units list

---

## 12. Process Snapshot

```bash
# Top 5 CPU consumers
ps aux --sort=-%cpu | head -6

# Top 5 memory consumers
ps aux --sort=-%mem | head -6

# Zombie processes
ps aux | awk '$8=="Z"' | wc -l
```

Report: top CPU and memory hogs (name + PID + %), zombie count. Flag ⚠️ if zombies > 0.

---

## Report Format

Produce a **single Telegram-compatible message** (keep under ~1000 chars for Telegram;
if longer, split into sections and label Part 1 / Part 2):

```
🖥 System Health Report — <hostname>  <timestamp>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔥 CPU:       0.45 / 0.32 / 0.28  (4 cores, 1.5 GHz) — 12% busy
🌡 Temp:      48.3 °C
🧠 Memory:    512 MB / 2.0 GB (25%)  swap: 0 / 512 MB
💾 Disk:      / 8.1/32 GB (25%)  /data ⚠️ 28/32 GB (88%)
⏱ Uptime:    3 days, 14:22
🔌 Services:  ssh ✅  docker ✅  nginx ✅  fail2ban ✅
🌐 Network:   eth0 192.168.1.10  gw 192.168.1.1  internet ✅
              Listening: 22 (sshd) 80 (nginx) 443 (nginx)
🔥 Firewall:  ufw ACTIVE (15 rules)
🔄 Updates:   3 pending ⚠️  (0 security)
🐧 Kernel:    6.1.21-v8+  aarch64  — 0 errors
📋 Logs (24h): auth failures: 4  OOM: none  disk errors: none
⚙ Top proc:  python3 (8% CPU)  node (4% CPU)
```

### Thresholds & Emoji Key

| Metric               | ⚠️ Warning          | ❌ Critical           |
|----------------------|---------------------|-----------------------|
| CPU temp             | > 70 °C             | > 85 °C               |
| RAM usage            | > 85%               | > 95%                 |
| Swap usage           | > 50%               | > 80%                 |
| Disk usage           | > 70%               | > 90%                 |
| Auth failures (24 h) | > 10                | > 100                 |
| Pending updates      | > 0                 | > 20                  |
| Service              | —                   | inactive (installed)  |
| Firewall             | disabled on public  | —                     |
| OOM events           | —                   | any                   |
| Disk I/O errors      | —                   | any                   |
| Zombie processes     | any                 | > 5                   |

---

## Notes

- Run all commands with `bash_tool`; use `file_read` for `/proc` and `/sys` paths.
- Skip any section gracefully if the tool or path is unavailable; note it as `—`.
- Prefer `journalctl` over flat log files when both are available.
- On Raspberry Pi, add `vcgencmd get_throttled` output and decode throttle flags:
  ```
  0x50005 → under-voltage detected, currently throttled
  ```
- If a section produces more than ~5 lines of data, summarise it (e.g., "15 rules" not
  the full iptables dump).
- If running as non-root, `iptables` and some `journalctl` commands may fail — note
  "insufficient permissions" and continue.
