## Why

The codebase maintains ~500 lines of hand-rolled MCP transport code (subprocess management, JSON-RPC 2.0 framing, SSE parsing, session tracking, pagination) that duplicates what the official `mcp` Python SDK provides. Replacing it with the SDK eliminates maintenance burden, gains protocol version negotiation, typed results, and future MCP features for free, with zero changes to any caller's API contract.

## What Changes

- Replace custom `MCPStdioClient` and `MCPHttpClient` transport implementations with the official `mcp` Python SDK (v1.27.0+)
- MCPManager internals rewritten to bridge the SDK's async API to the existing synchronous callers
- Remove `requests` dependency (only used by the old HTTP transport; grep confirms no other module imports it)
- Public API (`has_tool`, `call_tool`, `get_tools`, `list_servers`, `set_enabled`, `connect_all`, `close_all`) and `MCPManagerProtocol` remain identical
- Config format (`[[mcp_servers]]` in config.toml) and `MCPServerConfig` remain identical
- Connection-loss behaviour: on stdio subprocess death, the SDK's transport layer handles reconnection; if a tool call fails due to connection loss, the error is returned as a standard tool failure dict (no silent auto-restart as in the current implementation)

## Capabilities

### New Capabilities

- `mcp-transport`: MCP server communication — tool discovery, invocation, and result handling — uses the official `mcp` Python SDK for transport, protocol negotiation, and typed results. Includes connection-loss and error scenarios.

### Modified Capabilities

<!-- No existing spec-level behaviour changes. The MCP tool interface (has_tool, call_tool, get_tools) is unchanged. -->

## Impact

- `mcp_client.py`: ~750 lines → ~250 lines (rewrite internals, keep public API)
- `tests/test_mcp_client.py`: rewrite to match new internals
- `requirements.txt`: add `mcp>=1.27.0,<2.0`, remove `requests`
- No changes to: `interfaces.py`, `config_schema.py`, `config.toml`, `main.py`, `react_loop.py`, `telegram_commands.py`, `builtin_tools/schemas.py`, `exceptions.py`
