## Why

The agent's context window is consumed by three separate channels — system prompt, chat history, and tool definitions — but only the first two are measured. The native tool-calling path injects all 21 built-in tools plus every registered MCP tool into the LLM payload, and this cost is invisible to the compaction logic (`maybe_compact` counts only `system` + `messages`). This means compaction can trigger too late when tool definitions consume a large fraction of the window, and the operator has no visibility into which MCP servers are eating context. A continuously-running context monitor that tracks consumption by category, exposed via a Telegram command and a built-in tool, is the foundation for future context budgeting and automated event triggers.

## What Changes

- **New: `ContextMonitor` module** — a lightweight monitor that lives on `AgentController` and receives a push snapshot from `react_loop` each turn. Tracks token consumption by category: system prompt, chat history, tool definitions, completion reserve. Sub-categorises tool definitions by MCP server (built-in vs each MCP server) so "fat" servers are visible. Computes `danger_level` (safe / approaching / danger) and `headroom_real` (accounting for tool defs). Holds the last published snapshot when idle so `/context` works between runs.
- **Bug fix: `maybe_compact` tool-def invisibility** — `maybe_compact()` gains an optional `tool_defs_tokens` parameter (default 0 for backward compatibility). The `react_loop` call site computes the tool-definition token cost from `ctx._tool_defs` and passes it in. Compaction now accounts for the real payload size, not just system + messages.
- **New: `/context` Telegram command** — reads the latest snapshot from the monitor and renders a summary dashboard: tokens + percentages + bar chart for each category, tool defs grouped by MCP server, danger level, and real headroom. Works both mid-run (live snapshot) and idle (last snapshot). Registered as a visible command in the BotFather menu and help text.
- **New: `context_profile` built-in tool** — returns a compact JSON snapshot from the monitor. The agent can inspect its own context consumption. Informational only — no actions, no automatic triggers. The tool is not confirmation-capable.
- **Modified: `per-model-context-window`** — the compaction threshold formula now includes tool-definition tokens in the total, so the 85% margin is computed against the real payload size.
- **Modified: `telegram-command-surface`** — `/context` is added to the visible command surface and help text.
- **Modified: `builtin-tool-execution`** — `context_profile` is registered as a new built-in tool (not confirmation-capable, executed by the built-in executor dispatch).

## Capabilities

### New Capabilities
- `context-monitoring`: Continuous tracking of context-window consumption by category (system prompt, chat history, tool definitions, completion reserve) with per-MCP-server tool-def grouping, danger-level computation, and snapshot publication from the ReAct loop.

### Modified Capabilities
- `per-model-context-window`: The compaction threshold now accounts for tool-definition token cost, not just system + messages.
- `telegram-command-surface`: `/context` is added to the visible command discovery and help text.
- `builtin-tool-execution`: `context_profile` is registered as a new non-confirmation-capable built-in tool.

## Impact

- **New module**: `context_monitor.py` — `ContextMonitor` class, `ContextSnapshot` dataclass, `danger_level` computation.
- **Modified**: `context_manager.py` — `maybe_compact()` gains `tool_defs_tokens` parameter.
- **Modified**: `react_loop.py` — publishes snapshot to monitor each turn; computes and passes `tool_defs_tokens` to `maybe_compact`.
- **Modified**: `agent_controller.py` — holds `ContextMonitor` instance; exposes it to `ReactContext`, Telegram interface, and built-in executor.
- **Modified**: `agent_runtime.py` — `ControllerDeps` and `ReactContext` gain `context_monitor` field.
- **Modified**: `telegram_commands.py` — new `cmd_context` handler.
- **Modified**: `telegram_interface.py` — register `/context` command and BotCommand entry.
- **Modified**: `builtin_tools/descriptors.py` — add `context_profile` to `BUILTIN_TOOLS`.
- **Modified**: `builtin_tools/schemas.py` — add `context_profile` JSON schema to `BUILTIN_TOOL_SCHEMAS`; update `build_tool_definitions()`.
- **Modified**: `builtin_tools/` — new handler module for `context_profile` (or addition to existing module).
- **Modified**: `main.py` — construct `ContextMonitor`, wire to `AgentController`.
- **Tests**: new test module for `ContextMonitor`; updated tests for `maybe_compact` with `tool_defs_tokens`; new test for `cmd_context`; new test for `context_profile` tool.
- **Config**: no new config fields required (monitor is always on, lightweight).
- **Documentation**: README and config example updated with `/context` command and `context_profile` tool.