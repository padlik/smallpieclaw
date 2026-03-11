#!/bin/bash
# description: check disk usage and free space on all mounted filesystems

df -h | awk 'NR==1 || /^\/dev/'
echo ""
echo "--- Top 5 largest directories in / (may take a moment) ---"
du -h --max-depth=2 / 2>/dev/null | sort -rh | head -5
