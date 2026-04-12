#!/bin/bash
# description: show network interfaces, IP addresses, and internet connectivity
echo "=== Network Interfaces ==="
ip addr show 2>/dev/null || ifconfig 2>/dev/null || echo "ip/ifconfig not available"
echo ""
echo "=== Default Route ==="
ip route 2>/dev/null | grep default || echo "No default route found"
echo ""
echo "=== DNS Connectivity ==="
ping -c 2 -W 2 8.8.8.8 2>&1 | tail -3
