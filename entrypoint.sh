#!/bin/bash
set -e

echo "Starting TM Agent..."

# Initialize directories if they don't exist
mkdir -p /app/data /app/logs /app/tools/tools_generated

# Initialize tool index if it doesn't exist
if [ ! -f "/app/data/tool_index.json" ]; then
    echo '{"version": 1, "embedding_model": "", "tools": []}' > /app/data/tool_index.json
fi

# Initialize memory if it doesn't exist
if [ ! -f "/app/data/memory.json" ]; then
    echo '{"last_backup_date": null, "known_services": [], "previous_diagnostics": []}' > /app/data/memory.json
fi

# Initialize allowed IDs if it doesn't exist
if [ ! -f "/app/data/allowed_ids.json" ]; then
    echo '{"allowed_ids": []}' > /app/data/allowed_ids.json
fi

# Build tool index if needed
if [ ! -s "/app/data/tool_index.json" ] || [ "$(cat /app/data/tool_index.json)" = '{"version": 1, "embedding_model": "", "tools": []}' ]; then
    echo "Building initial tool index..."
    python -c "
from tool_registry import registry
from tool_index import index
registry.scan()
index.build_index([{'name': t['name'], 'description': t['description']} for t in registry.list_tools()])
" || echo "Tool index build failed - will retry on first run"
fi

# Start the agent
exec python -m agent.telegram_bot

