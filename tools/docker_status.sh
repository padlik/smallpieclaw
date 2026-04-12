#!/bin/bash
# description: list running and stopped Docker containers with their status and ports
if command -v docker &>/dev/null; then
  echo "=== Running Containers ==="
  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null
  echo ""
  echo "=== Stopped Containers ==="
  docker ps -a --filter "status=exited" --format "table {{.Names}}\t{{.Status}}" 2>/dev/null
else
  echo "Docker is not installed or not in PATH."
fi
