# context-monitoring Specification

## Purpose
Define informational context monitoring of estimated input tokens for system prompt, chat history, and tool definitions by MCP server, with the configured compaction threshold and model identity/liveness visible through `/context` and `context_profile`, without capacity, reserve, headroom, or danger-level calculations.

## Requirements

### Requirement: Context monitor tracks context-window consumption by category

`AgentController` SHALL hold a `ContextMonitor` of estimated input tokens by system prompt, chat history, and tool definitions. The main ReAct loop SHALL push a snapshot each turn; sub-agents SHALL NOT publish to it; it SHALL keep the last snapshot when idle. Snapshots SHALL carry counts, compaction threshold, turn, model identity, and `is_live`, never capacity data.

Feature: Context monitoring
Rule: The monitor is always on and lightweight — a reference-swap snapshot per turn, not a deep copy.

#### Scenario: Monitor receives snapshot each turn during a run
- **GIVEN** the main ReAct loop is running and the `AgentController` holds a `ContextMonitor`
- **WHEN** a turn completes
- **THEN** the loop SHALL publish a `ContextSnapshot` containing token estimates for system prompt, chat history, and tool definitions
- **AND** the snapshot SHALL include the configured compaction threshold and the current turn number
- **AND** the snapshot SHALL include the pinned model identity and `is_live` status for consumers
- **AND** the snapshot SHALL NOT include a completion reserve or effective context window

#### Scenario: Sub-agent runs do not overwrite the main snapshot
- **GIVEN** a sub-agent run is active while the main snapshot exists
- **WHEN** the sub-agent completes turns
- **THEN** the main monitor's snapshot is not replaced by sub-agent counts

#### Scenario: Monitor retains last snapshot when idle
- **GIVEN** the agent has completed a run and is now idle
- **WHEN** a context inspection is requested
- **THEN** the monitor SHALL return the last snapshot published during the run
- **AND** the snapshot SHALL be marked as not live

#### Scenario: Monitor reports no snapshot before first run
- **GIVEN** the agent has not yet executed any run
- **WHEN** a context inspection is requested
- **THEN** the monitor SHALL report that no snapshot is available

#### Scenario: Snapshot carries no capacity-derived data
- **GIVEN** the monitor holds a published snapshot
- **WHEN** its fields are inspected
- **THEN** it SHALL NOT contain a completion reserve, effective context window, inferred model capacity, capacity percentage, headroom, or danger level

### Requirement: Tool definitions sub-categorised by MCP server

The monitor SHALL group tool-definition token cost by source: built-ins as one group and each MCP server, seeded from all registered servers, by name. Built-ins SHALL be classified by name against `BUILTIN_TOOL_SCHEMAS` and `PSEUDO_TOOL_SCHEMAS`, never by the MCP-only `ToolRegistry`; tools absent from the registry and both schema name sets SHALL be `unknown`.

Feature: Context monitoring
Rule: Tool definitions arrive in batches per MCP server — the monitor groups them so fat servers are visible.

#### Scenario: Tool defs grouped by server
- **GIVEN** the agent has 21 built-in tools and MCP tools from servers "github", "filesystem", and "weather"
- **WHEN** the monitor publishes a snapshot
- **THEN** the snapshot SHALL include a per-server token breakdown
- **AND** built-in tools SHALL be grouped under a "builtin" label
- **AND** each MCP server's tools SHALL be grouped under that server's name
- **AND** the total tool-definition token cost SHALL be defined as the sum of all per-server group estimates
- **AND** this same total SHALL be the tool-definition part of the estimate compared with the compaction threshold

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

#### Scenario: Registered servers seed the grouping
- **GIVEN** the MCP manager or registry lists registered servers, some with no discovered tools
- **WHEN** the monitor groups tool definitions by server
- **THEN** the per-server grouping SHALL be seeded from that full list of registered servers

#### Scenario: Unclassified tools are reported as unknown
- **GIVEN** a tool definition is neither found in the registry nor named in the built-in and pseudo-tool schema name sets
- **WHEN** the monitor classifies it
- **THEN** it SHALL be classified as `unknown`

### Requirement: Context snapshot is a reference swap without locking

The monitor SHALL accept snapshots via a lightweight reference swap (no deep copy, no lock). The `ContextSnapshot` dataclass SHALL be immutable (`frozen=True`) so that published snapshots are never mutated after publication. The idle transition (live to not-live) SHALL publish a new snapshot with `is_live=False`, never mutate the existing one. The snapshot is a diagnostic view, not a transactional read — a slightly stale snapshot is acceptable. The monitor SHALL be thread-safe for concurrent reads from the Telegram event loop while the agent thread publishes snapshots.

Feature: Context monitoring
Rule: Diagnostic, not transactional — eventual consistency is fine for a context profile.

#### Scenario: Concurrent read during publish does not crash
- **GIVEN** the agent thread is publishing a snapshot to the monitor
- **WHEN** the Telegram event loop reads the monitor simultaneously
- **THEN** the read SHALL return either the previous snapshot or the new one
- **AND** no exception SHALL be raised

#### Scenario: Snapshot stores token counts not message copies
- **GIVEN** the ReAct loop holds `state.messages` and `ctx._tool_defs`
- **WHEN** the loop publishes a snapshot
- **THEN** the snapshot SHALL store token counts and metadata, not copies of the messages or tool definitions themselves

### Requirement: Context snapshot refreshable outside the ReAct loop

After `/mcp on`, `/mcp off`, `/mcp auth`, or `/mcp auth revoke`, the monitor SHALL recompute `tool_defs_by_server` and `tool_defs_tokens` from the current tool definitions and publish an updated snapshot via `dataclass_replace` on the last one, preserving its other counts, threshold, turn, and model identity, with `is_live=False`.

Feature: Context monitoring
Rule: Tool-defs data should be current even when the agent is idle — the operator needs accurate tool-defs breakdown after MCP changes.

#### Scenario: Snapshot refreshed after /mcp on
- **GIVEN** the agent is idle and the last snapshot shows `google-workspace: 0` tools
- **WHEN** the operator runs `/mcp on google-workspace` and the server's tools are registered
- **THEN** the context monitor SHALL publish an updated snapshot with the current `tool_defs_by_server` grouping
- **AND** `google-workspace` SHALL show a non-zero token count reflecting its tool definitions
- **AND** `tool_defs_tokens` SHALL be the sum of all per-server group estimates
- **AND** `system_prompt_tokens`, `chat_history_tokens`, `compaction_threshold`, and `turn` SHALL be unchanged from the last snapshot
- **AND** the last snapshot's model identity SHALL be unchanged
- **AND** `is_live` SHALL be `False`

#### Scenario: Snapshot refreshed after /mcp off
- **GIVEN** the agent is idle and the last snapshot shows `mcp-atlassian: 33,373` tokens
- **WHEN** the operator runs `/mcp off mcp-atlassian` and the server's tools are unregistered
- **THEN** the context monitor SHALL publish an updated snapshot
- **AND** `mcp-atlassian` SHALL show a token cost of zero
- **AND** `tool_defs_tokens` SHALL decrease by the removed tools' token cost

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

#### Scenario: Tool changes outside the loop support partial refresh
- **GIVEN** tool definitions change outside the ReAct loop
- **WHEN** the monitor refreshes its last published snapshot
- **THEN** it SHALL recompute `tool_defs_by_server` and `tool_defs_tokens` from the current definitions and publish a partial refresh via `dataclass_replace` on the last published snapshot
- **AND** `system_prompt_tokens`, `chat_history_tokens`, `compaction_threshold`, `turn`, and model identity SHALL be preserved from that snapshot
- **AND** the refreshed snapshot SHALL have `is_live=False`

### Requirement: /context Telegram command renders summary dashboard

The `/context` command SHALL render the latest monitor snapshot as a summary: pinned model identity, estimated tokens for system prompt, chat history, and tool definitions with their total, the compaction threshold, and tool definitions grouped by MCP server. It SHALL NOT show a context window, capacity percentage, completion reserve, headroom, or danger level, and SHALL work both mid-run and idle.

Feature: Context monitoring
Rule: Summary only — key points, not a detailed research view.

#### Scenario: /context shows dashboard with live snapshot
- **GIVEN** the agent is running and the monitor has a live snapshot
- **WHEN** an authorized operator runs `/context`
- **THEN** the response SHALL show the model name and live indicator
- **AND** the response SHALL show token counts for system prompt, chat history, and tool definitions, their total, and the compaction threshold
- **AND** the response SHALL show tool definitions grouped by MCP server
- **AND** the response SHALL NOT show a context window, percentage, completion reserve, headroom, or danger level

#### Scenario: /context shows dashboard with last snapshot when idle
- **GIVEN** the agent is idle and the monitor retains the last snapshot from the previous run
- **WHEN** an authorized operator runs `/context`
- **THEN** the response SHALL show the counts with a not-live indicator
- **AND** the response SHALL note that the snapshot is from the last run

#### Scenario: /context reports no snapshot available
- **GIVEN** the agent has not yet executed any run
- **WHEN** an authorized operator runs `/context`
- **THEN** the response SHALL state that no context snapshot is available yet

### Requirement: context_profile built-in tool returns JSON snapshot

The `context_profile` built-in tool SHALL return a compact JSON snapshot from the context monitor. The tool is informational only — it does not perform any action or trigger any automatic behavior. The tool is not confirmation-capable.

Feature: Context monitoring
Rule: The agent can inspect its own context consumption to make informed decisions about finishing early or avoiding sub-agents.

#### Scenario: context_profile returns snapshot
- **GIVEN** the agent calls the `context_profile` built-in tool
- **WHEN** the tool executes
- **THEN** the result SHALL be a JSON object containing total_tokens, compaction_threshold, breakdown by category, tool_defs_by_server, model identity, is_live, and turn
- **AND** `total_tokens` SHALL be the sum of system_prompt_tokens + chat_history_tokens + tool_defs_tokens
- **AND** the result SHALL include an `is_live` boolean indicating whether the snapshot is from a running turn
- **AND** the result SHALL NOT contain danger_level, headroom_real, completion_reserve, or effective_window

#### Scenario: context_profile is not confirmation-capable
- **GIVEN** the built-in executor
- **WHEN** the confirmation-capable built-ins are enumerated
- **THEN** `context_profile` SHALL NOT be in the confirmation-capable set

#### Scenario: context_profile is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("context_profile")` or lists `all_tools`
- **THEN** `context_profile` SHALL be reported as a built-in
