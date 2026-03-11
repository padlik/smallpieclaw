#!/bin/bash
# description: check network interfaces, IP addresses, and connectivity

echo "=== Network Interfaces ==="
ip addr show 2>/dev/null || ifconfig 2>/dev/null

echo ""
echo "=== Routing Table ==="
ip route 2>/dev/null || netstat -rn 2>/dev/null

echo ""
echo "=== Connectivity Test ==="
if ping -c 1 -W 3 8.8.8.8 &>/dev/null; then
    echo "✅ Internet: reachable (8.8.8.8)"
else
    echo "❌ Internet: not reachable"
fi

if ping -c 1 -W 3 1.1.1.1 &>/dev/null; then
    echo "✅ Cloudflare DNS: reachable"
fi

echo ""
echo "=== Listening Ports ==="
ss -tlnp 2>/dev/null | head -20 || netstat -tlnp 2>/dev/null | head -20
