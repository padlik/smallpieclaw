#!/bin/bash
# description: check disk usage across all mount points
echo "=== Disk Usage ==="
df -h --exclude-type=tmpfs --exclude-type=devtmpfs 2>/dev/null || df -h
