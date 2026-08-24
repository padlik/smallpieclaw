## Why

The `/context` command's "Tool defs by server" breakdown misclassifies built-in tools as `unknown` (because `group_tool_defs_by_server` looks up tool names in the MCP-only `ToolRegistry`, which never contains builtins), misclassifies post-OAuth MCP tools as `unknown` (because `_run_oauth_flow` registers tools in the MCPManager but not in the `ToolRegistry`), and shows stale tool-defs data when the agent is idle (because the snapshot is only published during the ReAct loop, not after tool-changing commands like `/mcp on`, `/mcp off`, `/mcp auth`). The operator sees `builtin: 0` and `unknown: 11,492` instead of the real breakdown, making it impossible to identify which MCP servers consume disproportionate context.

## What Changes

- **Fix builtin classification**: `group_tool_defs_by_server` classifies a tool as `builtin` when its name appears in `BUILTIN_TOOL_SCHEMAS` or `PSEUDO_TOOL_SCHEMAS`, even when the `ToolRegistry` returns `None`. This eliminates the `unknown` bucket for builtins without changing the `ToolRegistry` contract.
- **Fix post-OAuth tool registration**: After a successful `/mcp auth <name>` flow, the newly discovered tools are registered in the `ToolRegistry` via `register_mcp_tools`, mirroring what `/mcp on` already does. This ensures `group_tool_defs_by_server` can attribute OAuth-authenticated tools to their server.
- **Refresh snapshot on tool changes**: After `/mcp on`, `/mcp off`, `/mcp auth`, and `/mcp auth revoke`, the context monitor snapshot is partially refreshed — only `tool_defs_by_server`, `tool_defs_tokens`, `danger_level`, and `headroom_real` are recomputed and published via `dataclass_replace` on the last snapshot. System prompt tokens, chat history tokens, completion reserve, effective window, compaction threshold, and turn number are preserved from the last snapshot. The snapshot is marked `is_live=False`.

## Capabilities

### New Capabilities

_(none)_

### Modified Capabilities

- `context-monitoring`: Tool-definition grouping must classify builtins by schema name set, not by `ToolRegistry` lookup. The snapshot must be refreshable outside the ReAct loop when tool definitions change.
- `mcp-oauth-flow`: After a successful OAuth flow, the discovered tools must be registered in the `ToolRegistry` so they are attributable by server in the context profile.

## Impact

- **`context_monitor.py`**: `group_tool_defs_by_server` gains a `builtin_names` parameter (a `set[str]`) used to classify tools not found in the registry. The function imports nothing new — the caller passes the name set.
- **`react_loop.py`**: `_tool_defs_by_server_for_context` passes the builtin name set (built from `BUILTIN_TOOL_SCHEMAS` and `PSEUDO_TOOL_SCHEMAS`) to `group_tool_defs_by_server`.
- **`telegram_commands.py`**: `_mcp_auth` registers tools in the `ToolRegistry` after OAuth success. A new helper `_refresh_tool_defs_snapshot` recomputes and publishes a partial snapshot after `/mcp on`, `/mcp off`, `/mcp auth`, and `/mcp auth revoke`.
- **Tests**: `test_context_monitor.py` updated for the new `builtin_names` parameter. New tests for post-OAuth registration and snapshot refresh.