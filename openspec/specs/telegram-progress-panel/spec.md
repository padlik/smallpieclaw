# Telegram Progress Panel Specification

## Purpose

Define the compact progress panel rendered in Telegram during agent runs, including per-tool briefs, MCP tool markers, merged tool-call lines, shell live-tail, thinking duration display, step count limits, and LLM error card rendering.

## Requirements

### Requirement: Tool-call brief on Running line

The progress panel SHALL display a short per-tool brief on the Running line so the operator can see what the tool is doing, not just the tool name. The brief SHALL be extracted from the tool's arguments by a per-tool formatter (`fmt_tool_brief`) that selects the semantically meaningful argument for each tool. The brief SHALL be truncated to approximately 35 characters with an ellipsis suffix when it exceeds that length.

Feature: Telegram progress panel
Rule: Every tool call shows a one-line brief of what it's doing, not just the tool name.

#### Scenario: Shell command shows stripped command text
- **GIVEN** the agent calls the `shell` tool with `command = "sh -c \"python3 -c 'import json, sys; ...'\"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows the shell wrapper stripped (`sh -c` removed) and the inner command truncated to ~35 chars
- **AND** the line does not show the raw `sh -c` wrapper

#### Scenario: file_read shows basename
- **GIVEN** the agent calls `file_read` with `path = "/home/paul/projects/foo/config.yaml"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_read config.yaml`
- **AND** the line does not show the full directory path

#### Scenario: file_patch shows basename and line counts
- **GIVEN** the agent calls `file_patch` with `path = "app.py"`, `old_str` containing 3 lines, and `new_str` containing 12 lines
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_patch app.py +12 -3`
- **AND** the line does not show the patch content

#### Scenario: file_diff shows both basenames with arrow
- **GIVEN** the agent calls `file_diff` with `path_a = "src/app.py"` and `path_b = "tests/app.py"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_diff app.py ↔ app.py`
- **AND** the line shows both file basenames connected by `↔`
- **AND** the full directory paths are not shown

#### Scenario: file_write shows basename and content length
- **GIVEN** the agent calls `file_write` with `path = "app.py"` and `content` of 340 characters
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_write app.py (340)`
- **AND** the line does not show the content itself

#### Scenario: secret_get shows key only
- **GIVEN** the agent calls `secret_get` with `key = "API_KEY"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `secret_get API_KEY`
- **AND** the line does not show the secret value

#### Scenario: spawn_agent shows truncated task description
- **GIVEN** the agent calls `spawn_agent` with `task = "research React 19 breaking changes and summarize"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `spawn_agent "research React 19…`
- **AND** the task is truncated to ~30 characters

#### Scenario: wait_for_any_agent shows count when more than two
- **GIVEN** the agent calls `wait_for_any_agent` with `agent_ids = ["sa-abc", "sa-def", "sa-ghi"]`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `wait_for_any_agent [3 agents]`
- **AND** the individual agent IDs are not listed

#### Scenario: wait_for_any_agent shows IDs when two or fewer
- **GIVEN** the agent calls `wait_for_any_agent` with `agent_ids = ["sa-abc", "sa-def"]`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `wait_for_any_agent sa-abc, sa-def`

#### Scenario: schedule shows action and tag
- **GIVEN** the agent calls `schedule` with `action = "add"`, `tag = "daily-report"`, `cron = "*/8 * * * *"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `schedule add "daily-report" */8 * * * *`

#### Scenario: shell_env_set shows key only
- **GIVEN** the agent calls `shell_env_set` with `key = "GITHUB_TOKEN"` and `value = "ghp_abc123"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `shell_env_set GITHUB_TOKEN`
- **AND** the value is not displayed

#### Scenario: shell_env_list shows static label
- **GIVEN** the agent calls `shell_env_list` with no arguments
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `shell_env_list list env vars`

### Requirement: MCP tools marked with server name

The progress panel SHALL mark MCP tool calls with `[MCP:server_name]` so the operator can distinguish built-in tools from external MCP server tools. The server name SHALL be looked up via `mcp_manager.server_name_for_tool(tool_name)` at TOOL_START time.

Feature: Telegram progress panel
Rule: MCP tools are visually distinguished from built-in tools.

#### Scenario: MCP tool shows server name marker
- **GIVEN** the agent calls an MCP tool `open` provided by the `agent-browser` MCP server
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows the tool name, a compact args brief, and `[MCP:agent-browser]`

#### Scenario: MCP tool with no args shows server marker only
- **GIVEN** the agent calls an MCP tool `list_tabs` provided by the `agent-browser` server with no arguments
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `list_tabs [MCP:agent-browser]`

### Requirement: Unknown tool fallback brief

The progress panel SHALL render a generic fallback brief for tools that are neither built-in nor MCP-registered. The fallback SHALL show the tool name and a compact argument representation with no `[MCP]` marker.

Feature: Telegram progress panel
Rule: Unknown tools get a generic brief without an MCP marker.

#### Scenario: Unknown tool shows generic brief
- **GIVEN** the agent calls a tool `custom_tool` that is not in `BUILTIN_TOOLS` and not registered as an MCP tool
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `custom_tool` followed by a compact argument representation
- **AND** the line does not include an `[MCP:...]` marker

### Requirement: Merged TOOL_START and TOOL_END line

The progress panel SHALL merge the TOOL_START and TOOL_END into a single panel line. The TOOL_START message appends a new step with tag `TOOL_RUNNING`. The TOOL_END message updates that step in place by appending ✅ or ❌ to the existing HTML, instead of appending a new step.

Feature: Telegram progress panel
Rule: One line per tool call — Running and result on the same line.

#### Scenario: Successful tool call merges into one line
- **GIVEN** the agent calls `file_read` and the tool succeeds
- **WHEN** the TOOL_END progress message arrives
- **THEN** the panel updates the last TOOL_RUNNING step in place
- **AND** the step shows `Running: file_read config.yaml ✅`
- **AND** no new step is appended for the result

#### Scenario: Failed tool call merges into one line
- **GIVEN** the agent calls `shell` and the tool fails
- **WHEN** the TOOL_END progress message arrives
- **THEN** the panel updates the last TOOL_RUNNING step in place
- **AND** the step shows `Running: shell "python3 -c …" ❌`
- **AND** no new step is appended for the result

#### Scenario: TOOL_END without matching TOOL_START appends new step
- **GIVEN** a TOOL_END progress message arrives and no step has tag `TOOL_RUNNING`
- **WHEN** the panel processes the message
- **THEN** the panel appends a new step with the result (fallback to current behavior)

### Requirement: Shell live-tail preserved during execution, dropped on completion

The progress panel SHALL preserve the `__SHELL_CHUNK__` live-tail behavior while a shell command is running. When the TOOL_END message arrives, the tail SHALL be dropped and replaced by ✅ or ❌ to keep the merged line clean.

Feature: Telegram progress panel
Rule: Shell output tail is a live preview; the final status replaces it.

#### Scenario: Shell tail shows while running
- **GIVEN** the agent calls `shell` and streaming is enabled
- **WHEN** a `__SHELL_CHUNK__` progress message arrives during execution
- **THEN** the last step is updated in place to show the brief and the live tail
- **AND** the brief is preserved in the step

#### Scenario: Shell tail dropped on completion
- **GIVEN** the shell command finishes and a TOOL_END message arrives
- **WHEN** the panel processes the TOOL_END
- **THEN** the tail is removed from the step
- **AND** the step shows the brief and ✅ or ❌ only

### Requirement: Thinking duration shown retroactively

The progress panel SHALL show a duration on the Thinking line by retroactively patching the step when the next step arrives. The duration SHALL be computed as the elapsed time between the Thinking step and the next step. The panel SHALL NOT use a live-ticking timer.

Feature: Telegram progress panel
Rule: The operator sees how long the LLM thought between tool calls.

#### Scenario: Thinking duration appears when next step arrives
- **GIVEN** a Thinking step is displayed as `⚙️ Thinking…`
- **WHEN** the next step (a tool call) arrives 5 seconds later
- **THEN** the Thinking step is updated in place to show `⚙️ Thinking… 5s`
- **AND** the new tool-call step is appended after it

#### Scenario: Thinking step with no following step shows no duration
- **GIVEN** a Thinking step is the last step in the panel
- **WHEN** no further step arrives
- **THEN** the Thinking step shows `⚙️ Thinking…` without a duration

### Requirement: Panel shows last 5 steps

The progress panel SHALL display the last 5 steps instead of 10. With merged tool-call lines (one per call instead of two), 5 steps show approximately the same information density as the previous 10 steps.

Feature: Telegram progress panel
Rule: Compact panel for mobile screens.

#### Scenario: Panel renders 5 most recent steps
- **GIVEN** the agent has executed 8 tool calls
- **WHEN** the panel renders
- **THEN** only the last 5 steps are visible
- **AND** older steps are not shown

### Requirement: Unchanged subsystems

The progress panel change SHALL NOT modify verbose mode, the confirmation flow, `fmt_tool_call()`, `fmt_tool_result_progress()`, the `LogEvent` taxonomy, the `on_tool_trace` hook, or `build_panel()` rendering logic (beyond the `_MAX_STEPS` constant and `_steps` tuple shape).

Feature: Telegram progress panel
Rule: The change is scoped to the compact panel only; existing diagnostics and flows are preserved.

#### Scenario: Verbose mode still sends full args
- **GIVEN** verbose mode is enabled
- **WHEN** a tool call occurs
- **THEN** the full `fmt_tool_call()` output is sent as a separate top-level message
- **AND** the compact panel brief does not replace the verbose output

#### Scenario: Confirmation prompt still shows full description
- **GIVEN** a tool call requires confirmation
- **WHEN** the confirmation prompt is rendered
- **THEN** the full confirmation description is shown with Yes/No buttons
- **AND** the brief is not used in place of the confirmation description

#### Scenario: LogEvent taxonomy unchanged
- **GIVEN** a tool call starts and ends
- **WHEN** `log_event` emits TOOL_START and TOOL_END
- **THEN** the events are written to the structlog dual-sink only
- **AND** no new subscriber or notification mechanism is added

#### Scenario: on_tool_trace hook remains unwired for main agent
- **GIVEN** the main agent runs a tool call
- **WHEN** the tool trace is emitted
- **THEN** the `on_tool_trace` callback is not invoked for the main agent
- **AND** the hook remains available for sub-agent registry use only

### Requirement: LLM error card with inline retry/cancel buttons

The progress panel SHALL handle a new `__LLM_ERROR__:{token}:{json}` progress marker by rendering an error card with inline buttons. The JSON payload SHALL contain: error type, classified message, model name, current step, max steps, count of preserved tool results, truncated error detail (first 200 chars), and retryable flag. When retryable is true, the card SHALL show `[🔄 Retry]` and `[❌ Cancel]` buttons. When retryable is false, the card SHALL show only `[❌ Cancel]`. The callback data SHALL use the format `llm_retry:{token}:{response}` where response is "retry" or "cancel".

Feature: Telegram progress panel
Rule: LLM errors get a dedicated error card with actionable buttons, not a buried text message.

#### Scenario: Retryable error card renders with both buttons
- **GIVEN** a `__LLM_ERROR__` progress marker arrives with retryable=true
- **WHEN** the progress panel processes the marker
- **THEN** an error card is rendered showing the error type icon and message
- **AND** the card shows the model name, step/max-steps, and preserved tool results count
- **AND** the card shows truncated error detail (first 200 chars)
- **AND** the card has a [🔄 Retry] button with callback_data `llm_retry:{token}:retry`
- **AND** the card has a [❌ Cancel] button with callback_data `llm_retry:{token}:cancel`

#### Scenario: Non-retryable error card renders with only Cancel
- **GIVEN** a `__LLM_ERROR__` progress marker arrives with retryable=false
- **WHEN** the progress panel processes the marker
- **THEN** an error card is rendered showing the error type icon and message
- **AND** the card shows the model name, step/max-steps, and preserved tool results count
- **AND** the card has only a [❌ Cancel] button with callback_data `llm_retry:{token}:cancel`
- **AND** no [🔄 Retry] button is shown

#### Scenario: Error card is sent while typing indicator persists
- **GIVEN** the typing indicator is active and an `__LLM_ERROR__` marker arrives
- **WHEN** the error card is rendered
- **THEN** the typing indicator continues until the user responds or the retry timeout expires
- **AND** the error card is sent as a reply to the original message

#### Scenario: LLM error callback unblocks agent thread
- **GIVEN** an error card is showing and the agent thread is blocked
- **WHEN** the user presses [🔄 Retry] or [❌ Cancel]
- **THEN** the callback handler calls `confirmation.signal_retry(token, response)`
- **AND** the agent thread unblocks
- **AND** the error card is updated to show the user's choice