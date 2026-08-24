## MODIFIED Requirements

### Requirement: Tool definitions sub-categorised by MCP server

The context monitor SHALL sub-categorise tool-definition token cost by source: built-in tools as one group, and each MCP server as a separate group identified by its server name. The per-server grouping SHALL be seeded from the full list of registered MCP servers (from the MCP manager/registry), so that servers with zero discovered tools still appear with a token cost of zero. This enables identification of MCP servers that consume a disproportionate share of the context window.

Built-in tools SHALL be classified by checking the tool name against the set of known built-in and pseudo-tool schema names (`BUILTIN_TOOL_SCHEMAS` and `PSEUDO_TOOL_SCHEMAS`). This classification SHALL NOT depend on the `ToolRegistry`, which is MCP-only. Tools not found in the registry and not in the builtin name set SHALL be classified as `unknown`.

Feature: Context monitoring
Rule: Tool definitions arrive in batches per MCP server — the monitor groups them so fat servers are visible. Built-in tools are identified by name, not by registry membership.

#### Scenario: Tool defs grouped by server
- **GIVEN** the agent has 21 built-in tools and MCP tools from servers "github", "filesystem", and "weather"
- **WHEN** the monitor publishes a snapshot
- **THEN** the snapshot SHALL include a per-server token breakdown
- **AND** built-in tools SHALL be grouped under a "builtin" label
- **AND** each MCP server's tools SHALL be grouped under that server's name
- **AND** the total tool-definition token cost SHALL be defined as the sum of all per-server group estimates
- **AND** this same total SHALL be the value passed to `maybe_compact()` as `tool_defs_tokens`

#### Scenario: Built-in tools classified by schema name set, not registry
- **GIVEN** the `ToolRegistry` contains only MCP tools (no built-in tools are registered)
- **AND** the tool definitions list includes built-in tools such as "shell" and "memory_store"
- **WHEN** `group_tool_defs_by_server` classifies each tool definition
- **THEN** built-in tools whose names appear in `BUILTIN_TOOL_SCHEMAS` or `PSEUDO_TOOL_SCHEMAS` SHALL be grouped under "builtin"
- **AND** the `unknown` group SHALL NOT contain any built-in tools
- **AND** the `builtin` group SHALL contain all built-in and pseudo-tool definitions

#### Scenario: MCP server with no tools contributes zero
- **GIVEN** an MCP server is registered but has discovered zero tools
- **WHEN** the monitor publishes a snapshot
- **THEN** that server SHALL appear with a token cost of zero
- **AND** the server SHALL be listed so the operator can see it is connected but empty

## ADDED Requirements

### Requirement: Context snapshot refreshable outside the ReAct loop

The context monitor SHALL support partial snapshot refresh when tool definitions change outside the ReAct loop. After tool-changing Telegram commands (`/mcp on`, `/mcp off`, `/mcp auth`, `/mcp auth revoke`), the system SHALL recompute `tool_defs_by_server`, `tool_defs_tokens`, `danger_level`, and `headroom_real` from the current tool definitions and publish an updated snapshot via `dataclass_replace` on the last published snapshot. System prompt tokens, chat history tokens, completion reserve, effective window, compaction threshold, and turn number SHALL be preserved from the last snapshot. The refreshed snapshot SHALL be marked `is_live=False`.

Feature: Context monitoring
Rule: Tool-defs data should be current even when the agent is idle — the operator needs accurate tool-defs breakdown after MCP changes.

#### Scenario: Snapshot refreshed after /mcp on
- **GIVEN** the agent is idle and the last snapshot shows `google-workspace: 0` tools
- **WHEN** the operator runs `/mcp on google-workspace` and the server's tools are registered
- **THEN** the context monitor SHALL publish an updated snapshot with the current `tool_defs_by_server` grouping
- **AND** `google-workspace` SHALL show a non-zero token count reflecting its tool definitions
- **AND** `tool_defs_tokens` SHALL be the sum of all per-server group estimates
- **AND** `danger_level` and `headroom_real` SHALL be recomputed from the new `tool_defs_tokens`
- **AND** `system_prompt_tokens`, `chat_history_tokens`, `completion_reserve`, `effective_window`, `compaction_threshold`, and `turn` SHALL be unchanged from the last snapshot
- **AND** `is_live` SHALL be `False`

#### Scenario: Snapshot refreshed after /mcp off
- **GIVEN** the agent is idle and the last snapshot shows `mcp-atlassian: 33,373` tokens
- **WHEN** the operator runs `/mcp off mcp-atlassian` and the server's tools are unregistered
- **THEN** the context monitor SHALL publish an updated snapshot
- **AND** `mcp-atlassian` SHALL show a token cost of zero
- **AND** `tool_defs_tokens` SHALL decrease by the removed tools' token cost
- **AND** `danger_level` and `headroom_real` SHALL be recomputed

#### Scenario: Snapshot refreshed after /mcp auth success
- **GIVEN** the agent is idle and the last snapshot shows `google-workspace: 0` tools
- **WHEN** the operator runs `/mcp auth google-workspace` and the OAuth flow completes successfully
- **THEN** the newly discovered tools SHALL be registered in the `ToolRegistry`
- **AND** the context monitor SHALL publish an updated snapshot with the tools attributed to `google-workspace`
- **AND** `google-workspace` SHALL show a non-zero token count

#### Scenario: Snapshot refreshed after /mcp auth revoke
- **GIVEN** the agent is idle and the last snapshot shows `google-workspace` with a non-zero token count
- **WHEN** the operator runs `/mcp auth revoke google-workspace`
- **THEN** the context monitor SHALL publish an updated snapshot
- **AND** `google-workspace` SHALL show a token cost of zero
- **AND** `tool_defs_tokens` SHALL decrease by the removed tools' token cost

#### Scenario: No snapshot to refresh
- **GIVEN** the agent has not yet executed any run and no snapshot has been published
- **WHEN** a tool-changing command is executed
- **THEN** no snapshot refresh SHALL be attempted
- **AND** the next ReAct loop run SHALL publish a fresh snapshot with current tool definitions