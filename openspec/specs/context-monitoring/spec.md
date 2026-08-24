# context-monitoring Specification

## Purpose
Define the context-window consumption monitoring system that tracks token usage by category (system prompt, chat history, tool definitions, completion reserve), computes danger levels from real headroom, and exposes the data through a `/context` Telegram command and a `context_profile` built-in tool.

## Requirements

### Requirement: Context monitor tracks context-window consumption by category

The system SHALL maintain a `ContextMonitor` on the `AgentController` that continuously tracks context-window consumption across four categories: system prompt, chat history, tool definitions, and completion reserve. The monitor SHALL receive a push snapshot from the ReAct loop each turn, and SHALL retain the last published snapshot when the agent is idle so that context inspection works between runs.

Feature: Context monitoring
Rule: The monitor is always on and lightweight — a reference-swap snapshot per turn, not a deep copy.

#### Scenario: Monitor receives snapshot each turn during a run
- **GIVEN** the ReAct loop is running and the `AgentController` holds a `ContextMonitor`
- **WHEN** a turn completes
- **THEN** the loop SHALL publish a `ContextSnapshot` to the monitor containing token estimates for system prompt, chat history, tool definitions, and completion reserve
- **AND** the snapshot SHALL include the effective context window and compaction threshold
- **AND** the snapshot SHALL include the current turn number

#### Scenario: Monitor retains last snapshot when idle
- **GIVEN** the agent has completed a run and is now idle
- **WHEN** a context inspection is requested
- **THEN** the monitor SHALL return the last snapshot published during the run
- **AND** the snapshot SHALL be marked as not live

#### Scenario: Monitor reports no snapshot before first run
- **GIVEN** the agent has not yet executed any run
- **WHEN** a context inspection is requested
- **THEN** the monitor SHALL report that no snapshot is available

### Requirement: Tool definitions sub-categorised by MCP server

The context monitor SHALL sub-categorise tool-definition token cost by source: built-in tools as one group, and each MCP server as a separate group identified by its server name. The per-server grouping SHALL be seeded from the full list of registered MCP servers (from the MCP manager/registry), so that servers with zero discovered tools still appear with a token cost of zero. This enables identification of MCP servers that consume a disproportionate share of the context window.

Feature: Context monitoring
Rule: Tool definitions arrive in batches per MCP server — the monitor groups them so fat servers are visible.

#### Scenario: Tool defs grouped by server
- **GIVEN** the agent has 21 built-in tools and MCP tools from servers "github", "filesystem", and "weather"
- **WHEN** the monitor publishes a snapshot
- **THEN** the snapshot SHALL include a per-server token breakdown
- **AND** built-in tools SHALL be grouped under a "builtin" label
- **AND** each MCP server's tools SHALL be grouped under that server's name
- **AND** the total tool-definition token cost SHALL be defined as the sum of all per-server group estimates
- **AND** this same total SHALL be the value passed to `maybe_compact()` as `tool_defs_tokens`

#### Scenario: MCP server with no tools contributes zero
- **GIVEN** an MCP server is registered but has discovered zero tools
- **WHEN** the monitor publishes a snapshot
- **THEN** that server SHALL appear with a token cost of zero
- **AND** the server SHALL be listed so the operator can see it is connected but empty

### Requirement: Danger level computed from real headroom

The context monitor SHALL compute a `danger_level` field from the real headroom, where real headroom accounts for tool-definition tokens. The danger levels SHALL be: `safe` (total below 70% of threshold), `approaching` (70% to below 90% of threshold), and `danger` (90% or above of threshold).

Feature: Context monitoring
Rule: The danger level uses the real payload size (system + history + tool defs), not the nominal size (system + history only), so the operator sees the true risk.

#### Scenario: Safe when well below threshold
- **GIVEN** the total context consumption (system + history + tool defs) is 50% of the compaction threshold
- **WHEN** the monitor computes the danger level
- **THEN** the danger level SHALL be `safe`

#### Scenario: Approaching when near threshold
- **GIVEN** the total context consumption is 80% of the compaction threshold
- **WHEN** the monitor computes the danger level
- **THEN** the danger level SHALL be `approaching`

#### Scenario: Danger when at or above threshold
- **GIVEN** the total context consumption is 92% of the compaction threshold
- **WHEN** the monitor computes the danger level
- **THEN** the danger level SHALL be `danger`

#### Scenario: Real headroom accounts for tool defs
- **GIVEN** the nominal headroom (threshold minus system minus history) is 22,000 tokens
- **AND** tool definitions consume 18,000 tokens
- **WHEN** the monitor computes headroom_real
- **THEN** headroom_real SHALL be 4,000 tokens
- **AND** the snapshot SHALL include both nominal headroom and real headroom

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

### Requirement: /context Telegram command renders summary dashboard

The `/context` command SHALL read the latest snapshot from the context monitor and render a summary dashboard showing token consumption by category with percentages (relative to the effective context window) and bar charts, tool definitions grouped by MCP server, the danger level, and the real headroom. The command SHALL work both mid-run (live snapshot) and idle (last snapshot).

Feature: Context monitoring
Rule: Summary only — key points, not a detailed research view.

#### Scenario: /context shows dashboard with live snapshot
- **GIVEN** the agent is running and the monitor has a live snapshot
- **WHEN** an authorized operator runs `/context`
- **THEN** the response SHALL show the model name, effective context window, and live indicator
- **AND** the response SHALL show token counts and percentages for system prompt, chat history, tool definitions, and completion reserve
- **AND** the response SHALL show a bar chart for each category
- **AND** the response SHALL show the danger level and real headroom
- **AND** the response SHALL show tool definitions grouped by MCP server

#### Scenario: /context shows dashboard with last snapshot when idle
- **GIVEN** the agent is idle and the monitor retains the last snapshot from the previous run
- **WHEN** an authorized operator runs `/context`
- **THEN** the response SHALL show the dashboard with a not-live indicator
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
- **THEN** the result SHALL be a JSON object containing danger_level, total_tokens, compaction_threshold, headroom_real, breakdown by category, tool_defs_by_server, is_live, and turn
- **AND** `total_tokens` SHALL be the sum of system_prompt_tokens + chat_history_tokens + tool_defs_tokens (excluding completion_reserve)
- **AND** the result SHALL include an `is_live` boolean indicating whether the snapshot is from a running turn

#### Scenario: context_profile is not confirmation-capable
- **GIVEN** the built-in executor
- **WHEN** the confirmation-capable built-ins are enumerated
- **THEN** `context_profile` SHALL NOT be in the confirmation-capable set

#### Scenario: context_profile is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("context_profile")` or lists `all_tools`
- **THEN** `context_profile` SHALL be reported as a built-in