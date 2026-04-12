#!/bin/bash
# description: show RAM usage, free memory, and top memory-consuming processes
echo "=== Memory Usage ==="
free -h
echo ""
echo "=== Top Memory Consumers ==="
ps aux --sort=-%mem | awk 'NR<=8 {printf "%-25s %5s%% CPU %5s%% MEM\n", $11, $3, $4}'
