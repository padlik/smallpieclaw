# Explore Brief: remove-handwritten-tools

## Context

The agent has two parallel tool systems:

1. **Built-in tools** (`builtin_executor.py` + `builtin_tools/` subpackage) — always available, hardcoded in Python, with full confirmation gates, zone-based access control, lifecycle logging, and error classification.
2. **Hand-written tools** (`tools/` and `tools_generated/` directories, `tool_registry.py`, `tool_executor.py`, `tool_creator.py`) — `.sh`/`.py` scripts discovered by scanning directories, executed via subprocess, with a `create_tool` action that lets the LLM propose and persist new executable scripts.

The `shell` built-in already subsumes hand-written tools: any `.sh`/`.py` tool can be expressed as a `shell` command. Hand-written tools add no capability that `shell` doesn't already provide, and they lack the safety infrastructure (confirmation gates, zone access control, lifecycle logging) that built-in tools have.

The `create_tool` action is a security risk: it lets the LLM write executable code to disk (`tools_generated/`) that is then run via subprocess. Even with the dangerous-pattern blocklist in `ToolCreator`, this is an attack surface that shouldn't exist.

## Alternatives Rejected

1. **Deprecation (keep dirs, stop scanning)** — rejected. User wants clean removal, no half-measures. Dead config paths and empty directories create confusion.
2. **Fold MCP tools into MCPManager, delete ToolRegistry** — rejected for now. Three modules (`mcp_client.py`, `tool_index.py`, `telegram_commands.py`) depend on `ToolRegistry`'s interface. Stripping ToolRegistry to MCP-only is zero-churn for those callers. When MCP tool count grows significantly, revisit whether MCPManager should own its own registry.
3. **Keep `create_tool` with stricter validation** — rejected. The fundamental problem is LLM-authored executable code persisted to disk. No amount of validation makes this safe enough. The `shell` built-in covers one-off scripts; reusable automation should be built-in tools or MCP tools.
4. **Keep ToolIndex only for MCP, remove built-in indexing** — rejected. Built-in tools are static and always listed in the prompt, but keeping them in the index is harmless (cached embeddings, no re-embedding cost). Removing them adds churn for no benefit.

## Final Approach: Key Decisions

### Remove entirely (files)
- `tool_executor.py` — `ToolExecutor` class (subprocess runner for .sh/.py)
- `tool_creator.py` — `ToolCreator` class (LLM-proposed tool creation)
- `tools/` directory — example scripts (check_cpu.sh, check_disk.sh, etc.)
- `tools_generated/` directory — LLM-created tools landing zone

### Remove (code paths)
- `react_loop.py`: `_dispatch_create_tool()` function + the `create_tool` action branch (lines ~838-842, ~1367-1426)
- `react_loop.py`: the final `return ctx.executor.execute(tool_name, args)` fallback (line ~1364) — after removal, unknown tools return an error
- `builtin_tools/schemas.py`: the `create_tool` pseudo-tool schema (line ~443)
- `confirmation.py`: `request_tool_create()` method
- `main.py`: `ToolExecutor` and `ToolCreator` construction (lines ~116-118, ~269-270), `gen_tools_dir` makedirs (line ~220)
- `agent_controller.py`: `executor` and `creator` constructor params + ReactContext fields (`executor`, `creator`)
- `agent_runtime.py`: `executor` and `creator` params if present

### Strip to MCP-only (modify, don't remove)
- `tool_registry.py`:
  - Remove: `refresh()`, `_parse_tool()`, `register()`, `tools_dirs` param, `_DESC_START_RE`, `_DESC_CONT_RE` regexes
  - Keep: `register_mcp_tools()`, `unregister_mcp_server()`, `get()`, `all()`, `exists()`, `summary()`, `Tool` dataclass
  - Constructor: `__init__(self)` — no `tools_dirs` param, no `refresh()` call, empty `_registry`
- `tool_index.py`: no code changes needed — `registry.all()` now returns only MCP tools, `_builtin_tools()` unchanged
- `react_loop.py`: `ReactContext.executor` and `ReactContext.creator` fields removed

### Modify (prompt + config + docs)
- `prompt_builder.py`: remove `create_tool` from "Possible actions" section, remove tool-creation rules, remove "AVAILABLE TOOLS" section or keep for MCP tools only
- `prompt_loader.py`: same prompt template changes (if it has separate templates)
- `config_schema.py`: `tools_dir` and `generated_tools_dir` path fields become unused — remove from `PathsConfig` dataclass and parsing
- `vulture_whitelist.py`: remove entries for `ToolExecutor`, `ToolCreator`, removed `ToolRegistry` methods
- `AGENTS.md`: update module table (remove `tool_executor.py`, `tool_creator.py`; update `tool_registry.py` description), update conventions, update testing fixtures
- `README.md`: remove hand-written tools documentation section

### Keep as-is
- `builtin_executor.py` + `builtin_tools/*` — entire built-in tool system
- `mcp_client.py` — MCP transport (uses ToolRegistry for registration)
- `telegram_commands.py` — `/tools` still works (lists MCP tools), `/reindex` still works
- `telegram_callbacks.py` — unaffected
- `scheduler.py` — unaffected (uses `builtin_executor._exec_spawn_agent`)
- `tool_index.py` — semantic search for built-in + MCP tools

### Cross-module data flow (after removal)

```
LLM action: {"action":"tool","tool":"shell","args":{...}}
    │
    ▼
react_loop._dispatch_tool()
    │
    ├── vision_query → _exec_vision_query()
    ├── builtin_executor.is_builtin(name)? → builtin_executor.execute()
    ├── mcp_manager.has_tool(name)? → mcp_manager.call_tool()
    └── else → error "unknown tool"
```

No more `ctx.executor.execute()` fallback. Unknown tools return an error instead of hitting ToolExecutor.

### ToolRegistry after stripping

```
ToolRegistry()
    _registry: dict[str, Tool]  ← MCP tools only, populated by register_mcp_tools()
    register_mcp_tools(server_name, tools)
    unregister_mcp_server(server_name) → int
    get(name) → Optional[Tool]
    all() → list[Tool]
    exists(name) → bool
    summary() → str
```

No `tools_dirs`, no `refresh()`, no file scanning, no `register()` (manual register was only used by ToolCreator).

### Open questions

1. **`tools_dir` makedirs in main.py** — `os.makedirs(tools_dir, exist_ok=True)` and `os.makedirs(gen_tools_dir, exist_ok=True)` at startup. Remove both. But should we clean up existing `tools/` and `tools_generated/` dirs on first run after upgrade, or leave them as orphaned directories?
2. **`/tools` Telegram command** — currently lists all registered tools (hand-written + MCP). After removal, it lists only MCP tools. If no MCP servers configured, it shows "No tools registered." Is that the right message, or should it say "No MCP tools — use built-in tools"?
3. **`/reindex` Telegram command** — rebuilds ToolIndex. Still works (re-embeds built-in + MCP tools). No change needed, but worth confirming.
4. **Test fixtures** — `tmp_agent_dir` fixture creates `tools/` and `tools_generated/` dirs. Tests that construct `ToolRegistry` with `tools_dirs` param will break. Need to update fixtures and tests.
5. **`builtin_tools/patterns.py` line 28** — references `tools_generated/` in dangerous patterns. Remove this pattern entry since the directory no longer exists.