#!/bin/bash
# description: show recent system log entries and any errors or warnings

echo "=== Last 20 system log entries ==="
if command -v journalctl &>/dev/null; then
    journalctl -n 20 --no-pager 2>/dev/null || tail -20 /var/log/syslog 2>/dev/null
else
    tail -20 /var/log/syslog 2>/dev/null || tail -20 /var/log/messages 2>/dev/null || echo "No system log found."
fi

echo ""
echo "=== Recent errors (last 50 lines) ==="
if command -v journalctl &>/dev/null; then
    journalctl -p err -n 10 --no-pager 2>/dev/null
else
    grep -i "error\|crit\|emerg" /var/log/syslog 2>/dev/null | tail -10 || echo "No errors found."
fi
