#!/bin/bash
# tool: docker_status
# description: show docker containers status
if command -v docker >/dev/null 2>&1; then
  docker ps -a
else
  echo "docker not installed"
fi

