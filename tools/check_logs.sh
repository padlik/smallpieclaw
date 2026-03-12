#!/bin/bash
# description: show recent system errors and warnings from journal or syslog
echo "=== Recent System Errors/Warnings ==="
journalctl -p warning --since "24 hours ago" --no-pager -n 50 2>/dev/null \
  || tail -n 50 /var/log/syslog 2>/dev/null \
  || echo "No log access available."
