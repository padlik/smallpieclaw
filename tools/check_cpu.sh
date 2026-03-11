#!/bin/bash
# description: check CPU usage, load average, and memory usage

echo "=== CPU Load ==="
uptime

echo ""
echo "=== Memory Usage ==="
free -h

echo ""
echo "=== Top 5 CPU-hungry processes ==="
ps aux --sort=-%cpu | head -6

echo ""
echo "=== Top 5 Memory-hungry processes ==="
ps aux --sort=-%mem | head -6
