# Design: remove-handwritten-tools

## Context

The agent currently dispatches tools through four paths in `_dispatch_tool()`:

1. `vision_query` → special-case handler in the ReAct loop (needs LLM access)
2. `builtin_executor.is_builtin(name)` → `BuiltinExecutor.execute()` (built-in tools)
3. `mcp_manager.has_tool(name)` → `MCPManager.call_tool()` (MCP tools)
4. Fallback → `ctx.executor.execute(tool_name, args)` (hand-written tools via `ToolExecutor`)

Path 4 is the one being removed. After removal, unknown tools (not built-in, not MCP, not vision_query) return an error result instead of hitting `ToolExecutor`.

The `create_tool` action is a separate dispatch path in the ReAct loop (`_dispatch_create_tool()`), intercepted before `_dispatch_tool()` for both text-based and native tool calling. This is also removed.

## Design Decisions

### D1: ToolRegistry becomes MCP-only (no interface change for callers)

`ToolRegistry` is stripped of file-scanning but keeps its public interface for MCP tool registration. Three modules depend on this interface:

```
mcp_client.py    → registry.register_mcp_tools(server_name, tools)
tool_index.py    → registry.all()  (now returns only MCP tools)
telegram_commands.py → registry.all()  (for /tools listing)
```

None of these callers change. The `Tool` dataclass stays (MCP tools are `Tool` objects with `is_mcp=True`).

**Constructor change:** `ToolRegistry(tools_dirs=[...])` → `ToolRegistry()`. No arguments, empty `_registry` dict, no `refresh()` call.

**Removed methods:** `refresh()`, `_parse_tool()`, `register()` (manual register was only used by `ToolCreator`).

**Removed module-level state:** `_DESC_START_RE`, `_DESC_CONT_RE` regexes, `os`/`re` imports if now unused.

### D2: Unknown tool dispatch returns an error result

The final fallback in `_dispatch_tool()`:

```python
# Registered tools
return ctx.executor.execute(tool_name, args)
```

becomes:

```python
# Unknown tool — no hand-written tools exist anymore
return {
    "success": False,
    "output": "",
    "error": f"Tool '{tool_name}' is not a built-in tool, MCP tool, or vision_query.",
    "exit_code": -1,
}
```

This matches the existing error pattern in `ToolExecutor._execute_impl()` (line 124-130) and `BuiltinExecutor._dispatch()` (line 355), which return error dicts rather than raising.

### D3: ReactContext loses `executor` and `creator` fields

`ReactContext` is a dataclass in `react_loop.py`. The `executor` and `creator` fields are removed. All construction sites must drop these kwargs:

- `main.py` — imports `ToolExecutor` and `ToolCreator` (lines 116-117), constructs them (lines 269-270), and passes `executor=`/`creator=` to `AgentController` (lines 325-326). All removed.
- `agent_controller.py` — `AgentController.__init__()` loses `executor` and `creator` params; `run()` stops passing them to `ReactContext`
- `agent_runtime.py` — `AgentRuntime.__init__()` loses `executor` and `creator` params; `build_react_context()` drops the kwargs; `SubAgentRunner` construction drops the kwargs
- `tests/execution_harness.py` — `run_react()` drops the kwargs
- All tests passing `executor=MagicMock()` or `creator=MagicMock()` — drop the kwargs

Since `ReactContext` is a dataclass with type annotations (not `Optional`), removing the fields means any construction site that still passes them gets a `TypeError`. This is a hard failure that makes missed sites immediately visible.

### D4: `create_tool` removal — all surfaces

The `create_tool` action exists in seven places:

1. **`react_loop.py`** — `_dispatch_create_tool()` function (lines ~1367-1426), the `create_tool` action branch in the main action dispatch (lines ~838-842), the native-tool-calling interception for `create_tool`, the "Unknown action" message at line 863 (still says `Use "tool", "create_tool", or "finish"` — update to drop `create_tool` and add `plan`), and the stale intercept comment at line 461. All removed/updated.

2. **`builtin_tools/schemas.py`** — `create_tool` entry in `PSEUDO_TOOL_SCHEMAS` (line ~443) and module docstring (line 5, mentions "pseudo-tools (create_tool, plan)"). Both removed/updated.

3. **`confirmation.py`** — full tool-creation confirmation subsystem: `request_tool_create()` method, `signal_tool_create()` method, `get_pending_tool_create()` method, state dicts (`_tool_create_events`, `_tool_create_results`, `tool_create_pending`), and class docstring §3 ("Tool creation"). All removed.

4. **`agent_controller.py`** — `get_pending_tool_create()` and `resume_tool_create()` passthrough methods (lines 237-243), module docstring at line 10 ("Dispatch action: tool | create_tool | finish" → "tool | plan | finish"), and SubAgentRunner docstring at line ~444 (references `ToolExecutor`, `ToolCreator`). All removed/updated.

5. **`telegram_callbacks.py`** — `cb_tool_create` handler (line 136) and `tool_create_yes/run/no` branches. Removed.

6. **`telegram_interface.py`** — `cb_tool_create` import (line 51), handler registration `^tool_create_` pattern (line 266), `_send_tool_create_prompt()` method (lines 742+), and "Create Tool"/"Run Once" buttons (lines 763-765). All removed.

7. **`prompt_builder.py`** and **prompt templates** — `create_tool` in the "Possible actions" section and tool-creation rules. The prompt templates live in `prompts/system/05-response-format.md` (line 27: `create_tool` action definition), `prompts/system/04-execution.md` (line 30: tool-creation rule), `prompts/system/03-capabilities.md` (line 14: "prefer these before creating new tools"), and `prompts/sub-agent/04-response-format.md` (line 14: separate "Possible actions" list). All updated.

### D5: Config path removal — `tools_dir` and `generated_tools_dir`

`config_schema.py` has `PathsConfig` with `tools_dir` and `generated_tools_dir` fields. These are removed from the dataclass and from the parsing function. Existing `config.toml` files with these keys will have them silently ignored (TOML parsing doesn't fail on extra keys).

`main.py` reads `paths.get("tools_dir", "tools")` and `paths.get("generated_tools_dir", "tools_generated")` — these lines are removed, along with `os.makedirs(tools_dir, exist_ok=True)` and `os.makedirs(gen_tools_dir, exist_ok=True)`.

### D6: `builtin_tools/patterns.py` — remove `tools_generated/` pattern

The dangerous-pattern list in `patterns.py` includes:

```python
(r"tools_generated/", "write/execute in tools_generated/ (same as tool creation — requires operator approval)"),
```

This entry is removed. The `tools/` directory is not in the pattern list (only `tools_generated/` was), so no other change is needed here.

### D7: Prompt changes

The system prompt in `prompt_builder.py` (and `prompt_loader.py` if separate templates) has:

1. "BUILT-IN TOOLS" section — unchanged (lists built-in tools)
2. "AVAILABLE TOOLS" section — kept, now populated only by MCP tools from `tool_index.search()`
3. "Possible actions" — `create_tool` action removed, leaving `tool` and `finish`
4. Tool-creation rules — removed ("Always try shell / file_read / file_write before proposing a new tool", "Propose a new tool ONLY when...", etc.)
5. The instruction "Use the shell tool for one-off or task-specific scripts — do NOT create a tool for single-use tasks" — removed (no create_tool to misuse)

### D8: Specs modified

**`native-tool-calling` spec:**
- The "Special-case tool interception" requirement currently lists `create_tool`, `plan`, and `vision_query`. After removal, only `plan` and `vision_query` are intercepted.
- The `create_tool` interception scenario is removed.
- The tool definition assembly requirement mentions `PSEUDO_TOOL_SCHEMAS` containing `create_tool` and `plan`. After removal, only `plan` remains.
- The "Script tools excluded" scenario (spec line 96-99) has a GIVEN precondition that script tools (`.sh`/`.py`) exist in the tool registry. After this change, the registry is MCP-only and can never hold script tools, making the scenario's precondition unreachable. This scenario is removed — the exclusion it describes is now a structural property (no script tools can exist in the registry) rather than a runtime filter.

**`file-access-zones` spec:**
- The agent-internal directory list at line 14 includes both `tools/` and `tools_generated/`. After removal, **both** are dropped from the list. The remaining agent-internal directories are: `data/`, `skills/`, `prompts/`, log dir, vault dir.

### D9: vulture_whitelist.py cleanup

`vulture_whitelist.py` has entries for public API symbols that vulture would flag as unused. After removing `ToolExecutor`, `ToolCreator`, and several `ToolRegistry` methods, the corresponding whitelist entries become stale and should be removed. This is mechanical — vulture itself will flag any leftover entries as unused, and the `make lint` gate (`vulture . vulture_whitelist.py --min-confidence 80`) will catch missed entries. No design decision needed beyond "remove entries for deleted symbols."

## Cross-module data flow (after removal)

```
LLM emits action
    │
    ├── {"action":"tool","tool":"<name>","args":{...}}
    │   └── react_loop._dispatch_tool()
    │       ├── vision_query → _exec_vision_query()
    │       ├── builtin_executor.is_builtin(name) → builtin_executor.execute()
    │       ├── mcp_manager.has_tool(name) → mcp_manager.call_tool()
    │       └── else → error result {"success": False, "error": "Tool '<name>' is not a built-in tool, MCP tool, or vision_query."}
    │
    ├── {"action":"plan", ...}  → plan execution path (unchanged)
    │
    └── {"action":"finish", ...} → finish (unchanged)
```

No `create_tool` action. No `ctx.executor.execute()` fallback.

## Risks

### R1: Missed ReactContext construction sites

Removing `executor`/`creator` from the dataclass causes `TypeError` at any construction site that still passes them. This is a hard failure — good for catching missed sites, but means the change must be applied atomically across all construction sites. The Tests & Fixtures section in the proposal enumerates the known sites.

**Mitigation:** `ruff check .` will not catch this (it's a runtime kwarg error, not a static type error). The test suite (`make test`) is the primary safety net. If any construction site is missed, the first test that exercises it fails immediately.

### R2: `add-nsjail-shell-isolation` overlap

The in-progress `add-nsjail-shell-isolation` change (0/48 tasks) modifies `builtin_tools/patterns.py` to add category tags to dangerous patterns. This change removes the `tools_generated/` pattern entry from the same file.

**Mitigation:** If this change lands first, the nsjail change's task "add category to all 15 patterns" becomes "add category to all 14 patterns" (one fewer entry). No conflict — just a count adjustment. If the nsjail change lands first, this change removes one already-categorized pattern entry. Either order works.

### R3: Orphaned directories on disk

Existing `tools/` and `tools_generated/` directories on deployed agents are left in place. The agent no longer scans them, so their contents are inert. Operators can delete them manually.

**Mitigation:** No automated cleanup. The README and AGENTS.md updates note that these directories are no longer used.

## What is NOT changing

- Built-in tool system (`builtin_executor.py`, `builtin_tools/*`) — tool logic and dispatch unchanged; `schemas.py` and `patterns.py` receive doc/pattern edits (see Modified)
- MCP transport (`mcp_client.py`) — untouched, still registers tools via `ToolRegistry`
- Semantic tool index (`tool_index.py`) — untouched, still indexes built-in + MCP tools
- Telegram commands (`telegram_commands.py`) — `/tools` and `/reindex` still work
- Scheduler (`scheduler.py`) — untouched, uses `builtin_executor._exec_spawn_agent`
- Sub-agent supervision (`sub_agent_supervisor.py`) — untouched
- Confirmation flow for built-in tools — untouched (only `create_tool` confirmation is removed)