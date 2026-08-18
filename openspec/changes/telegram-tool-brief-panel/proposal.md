## Why

During agent execution, the Telegram progress panel shows tool calls as two separate lines per tool (TOOL_START then TOOL_END) with only the tool name — arguments are generated but stripped before rendering. The user sees `Running: shell` with no indication of *what* command is running. With 10 visible steps and 2 lines per tool call, the panel shows ~5 tool calls and feels noisy. This change makes the panel more informative by showing a short per-tool brief, merging start/end into one line, and reducing visible steps to 5.

## What Changes

- Add `fmt_tool_brief()` — a per-tool formatter that extracts the semantically meaningful argument (file path, command, key, task, etc.) and renders it as a short one-line brief (~35 chars). Covers all 21 built-in tools with a generic fallback for MCP tools.
- Strip shell wrapper noise (`sh -c`, `cd X &&`, `export VAR=… &&`) before truncating the command, so the brief shows the actual work, not the plumbing.
- Mark MCP tools with `[MCP:server_name]` in the brief, using `ToolRegistry.get()` to look up the server name at TOOL_START time.
- Modify `_ProgressPanel.classify()` to keep the brief instead of stripping everything after the tool name.
- Merge TOOL_START and TOOL_END into a single panel line: TOOL_START appends a step, TOOL_END updates that step in place with ✅/❌ (reusing the `__SHELL_CHUNK__` in-place update pattern).
- Add a `tag` field to the `_steps` tuple so the panel can distinguish TOOL_RUNNING steps from THINKING steps for retroactive updates.
- Show `Thinking… Ns` duration by retroactively patching the Thinking step when the next step arrives (not a live timer).
- Reduce `_MAX_STEPS` from 10 to 5 (one line per tool call shows the same information density as 10 two-line steps).
- Preserve `__SHELL_CHUNK__` live-tail behavior while a shell command is running; drop the tail on TOOL_END to keep the merged line clean.
- Leave verbose mode, confirmation flow, `fmt_tool_call()`, `fmt_tool_result_progress()`, and `LogEvent` taxonomy unchanged.

## Capabilities

### New Capabilities
- `telegram-progress-panel`: Operator-visible progress panel behavior during agent execution — tool-call briefs, merged start/end lines, Thinking duration, step count, and MCP tool marking.

### Modified Capabilities
<!-- No existing spec-level behaviour changes. The telegram-command-surface spec covers slash commands, not the progress panel. -->

## Impact

- **`react_loop.py`** — new `fmt_tool_brief()` function (~60 lines); modified TOOL_START progress emission at line 1393 to include brief + MCP marker; MCP server-name lookup before emission.
- **`telegram_interface.py`** — modified `_ProgressPanel`: `classify()` keeps brief; `_steps` tuple gains a tag; `dispatch_progress()` merges TOOL_END into last step; Thinking duration retroactive patch; `_MAX_STEPS` 10→5; `__SHELL_CHUNK__` handler preserves brief.
- **`tool_registry.py`** — read-only usage (already exposes `Tool.server_name` via `ToolRegistry.get()`).
- No new dependencies. No breaking changes to external APIs. Verbose mode and confirmation flow are unaffected.