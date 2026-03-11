#!/bin/bash
# description: check Docker container status, images, and resource usage

if ! command -v docker &>/dev/null; then
    echo "Docker is not installed."
    exit 0
fi

if ! docker info &>/dev/null 2>&1; then
    echo "Docker daemon is not running."
    exit 0
fi

echo "=== Running Containers ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"

echo ""
echo "=== All Containers ==="
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"

echo ""
echo "=== Docker Disk Usage ==="
docker system df

echo ""
echo "=== Docker Images ==="
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
