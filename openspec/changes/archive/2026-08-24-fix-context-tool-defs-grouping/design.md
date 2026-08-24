## Context

The `/context` command's "Tool defs by server" breakdown has three bugs:

1. **Builtins misclassified as `unknown`**: `group_tool_defs_by_server` (`context_monitor.py:123`) looks up each tool name in the `ToolRegistry`, which is MCP-only — builtins are never registered. Every builtin tool falls through to `unknown`.

2. **Post-OAuth MCP tools misclassified as `unknown`**: `_run_oauth_flow` (`mcp_client.py:1577`) calls `_register_wrapper` (updates MCPManager's internal maps) but never calls `registry.register_mcp_tools()` (updates ToolRegistry). The `/mcp on` path does call it (`telegram_commands.py:1144`), but the OAuth path was missed.

3. **Stale snapshot when idle**: The context monitor snapshot is only published during the ReAct loop (`react_loop.py:1437,1452`). After tool-changing commands (`/mcp on`, `/mcp off`, `/mcp auth`, `/mcp auth revoke`), the snapshot retains the old tool-defs grouping until the next run.

ADR-0022 established the push-model context monitor. This change fixes classification and adds partial refresh — it does not change the push model itself.

## Goals / Non-Goals

**Goals:**
- Built-in tools appear under `builtin` in the tool-defs breakdown, not `unknown`.
- OAuth-authenticated MCP tools appear under their server name, not `unknown`.
- The context snapshot reflects current tool definitions after tool-changing commands, even when the agent is idle.

**Non-Goals:**
- Registering builtins in the `ToolRegistry` (the registry stays MCP-only).
- Full snapshot recomputation on tool changes (system prompt and chat history are preserved from the last run).
- Snapshot refresh on `/model` switches (effective window change — out of scope).
- Mid-run tool-defs cache invalidation (acceptable tradeoff per user decision).

## Decisions

### Decision 1: Classify builtins by schema name set, not registry lookup

`group_tool_defs_by_server` gains a `builtin_names: set[str] | None = None` parameter. When the `ToolRegistry` returns `None` for a tool name, the function checks if the name is in `builtin_names`. If yes → `builtin`. If no → `unknown`.

```
Current:                          Proposed:
  tool = registry.get(name)         tool = registry.get(name)
  if tool:                          if tool:
    if is_mcp: → server               if is_mcp: → server
    else: → builtin                   else: → builtin
  else: → unknown                   elif name in builtin_names: → builtin
                                    else: → unknown
```

**Why not register builtins in the ToolRegistry?** The registry's contract is MCP-only (docstring: "Tools are added only via register_mcp_tools"). Changing that contract has wider blast radius (tool indexing, dedup, search). Passing a name set is a one-parameter change with no contract impact.

**Why pass the set rather than import it in context_monitor.py?** `context_monitor.py` is intentionally decoupled from the rest of the agent code (module docstring). It already imports `token_estimator`, but importing `builtin_tools.schemas` would pull in the full schema definitions — heavier and less stable. The caller builds the set once and passes it.

The caller (`_tool_defs_by_server_for_context` in `react_loop.py`) builds the set from `BUILTIN_TOOL_SCHEMAS.keys() | PSEUDO_TOOL_SCHEMAS.keys()` — these are module-level constants that never change during a run.

### Decision 2: Register OAuth tools in ToolRegistry from the Telegram handler

After `start_oauth_flow` returns `{"success": True}` in `_mcp_auth` (`telegram_commands.py:1305`), the handler calls `iface.tool_registry.register_mcp_tools(name, info["tools"])` — the same call `/mcp on` makes at line 1144.

**Why in the Telegram handler, not in `_run_oauth_flow`?** The MCPManager (`mcp_client.py`) does not hold a reference to the `ToolRegistry`. Wiring one in would add a dependency from the MCP layer to the registry layer. The Telegram handler already has both references (`iface.tool_registry` and `iface.mcp_manager`) and already does this for `/mcp on`. Mirroring that pattern is 3 lines with no new wiring.

### Decision 3: Partial snapshot refresh via dataclass_replace

A new helper `_refresh_tool_defs_snapshot(iface)` in `telegram_commands.py` recomputes only the tool-defs-related fields and publishes an updated snapshot:

```
┌─────────────────────────────────────────────────────────────┐
│  _refresh_tool_defs_snapshot(iface)                        │
│                                                             │
│  1. last = monitor.read()                                   │
│     if last is None: return  (no snapshot to update)        │
│                                                             │
│  2. fresh = group_tool_defs_by_server(                      │
│         build_tool_definitions(mcp_manager),                │
│         tool_registry,                                      │
│         mcp_manager,                                        │
│         builtin_names,                                       │
│     )                                                       │
│     fresh_tokens = sum(fresh.values())                      │
│                                                             │
│  3. updated = dataclass_replace(                            │
│         last,                                               │
│         tool_defs_by_server=fresh,                          │
│         tool_defs_tokens=fresh_tokens,                      │
│         danger_level=compute_danger_level(                  │
│             last.system_prompt_tokens                       │
│             + last.chat_history_tokens                       │
│             + fresh_tokens,                                 │
│             last.compaction_threshold),                     │
│         headroom_real=compute_headroom_real(                │
│             last.compaction_threshold,                      │
│             last.system_prompt_tokens,                      │
│             last.chat_history_tokens,                       │
│             fresh_tokens),                                  │
│         is_live=False,                                      │
│     )                                                       │
│     monitor.publish(updated)                                │
└─────────────────────────────────────────────────────────────┘
```

**Why partial, not full?** A full snapshot needs `state.messages`, `system`, and the active model config — none of which are available outside the ReAct loop without significant plumbing. The tool-defs fields are the only ones that changed; the rest are preserved. The `is_live=False` marker tells the operator the snapshot is idle.

**Why dataclass_replace?** Already used in the codebase (`react_loop.py:1484` for the idle transition). `ContextSnapshot` is `frozen=True`, so `dataclass_replace` is the idiomatic way to produce a modified copy.

Called from: `_mcp_on`, `_mcp_off`, `_mcp_auth` (after success), `_mcp_auth_revoke`.

### Component diagram

```
┌─ Telegram Event Loop ─────────────────────────────────────────┐
│                                                                │
│  /mcp on ──┐                                                   │
│  /mcp off ─┤                                                   │
│  /mcp auth ┼─→ _refresh_tool_defs_snapshot(iface)             │
│  /mcp rev ─┘         │                                         │
│                      │ 1. read last snapshot                   │
│                      │ 2. recompute tool_defs_by_server        │
│                      │ 3. dataclass_replace + publish           │
│                      │                                         │
│  /context ──→ cmd_context ──→ monitor.read() ──→ render        │
│                                                                │
└────────────────────────────────────────────────────────────────┘
        │              │
        │              │
        ▼              ▼
┌─ ToolRegistry ─┐  ┌─ ContextMonitor ─────────────┐
│  (MCP-only)     │  │  _snapshot: ContextSnapshot  │
│                 │  │  publish() / read()          │
│  register_mcp_  │  └───────────────────────────────┘
│    tools()       │
│  unregister_mcp_ │  ┌─ React Loop (agent thread) ────────────┐
│    server()      │  │                                          │
└─────────────────┘  │  _publish_context_snapshot()             │
                     │    group_tool_defs_by_server(             │
┌─ MCPManager ──────┐│      tool_defs, registry, mcp_manager,   │
│  _wrappers         ││      builtin_names                       │
│  _tool_to_server   ││    ) → tool_defs_by_server               │
│  get_tools()       ││    → publish(snapshot)                   │
│  list_servers()    ││                                          │
└───────────────────┘└──────────────────────────────────────────┘

         ┌─ builtin_tools/schemas.py ─────────────────┐
         │  BUILTIN_TOOL_SCHEMAS: dict[str, ...]       │
         │  PSEUDO_TOOL_SCHEMAS: dict[str, ...]         │
         │  build_tool_definitions(mcp_manager) → list  │
         └─────────────────────────────────────────────┘
                      │
          builtin_names = BUILTIN_TOOL_SCHEMAS.keys()
                        | PSEUDO_TOOL_SCHEMAS.keys()
```

## Risks / Trade-offs

- **[Stale system/chat fields after refresh]** → The refreshed snapshot preserves system_prompt_tokens and chat_history_tokens from the last run. If the operator runs `/context` after `/reset` (which clears chat history), the chat history tokens will be stale. Mitigation: the `is_live=False` marker and "last run turn N" label make the staleness visible. A full fix would require plumbing messages + system prompt into the refresh, which is out of scope.

- **[builtin_names set could drift from actual tool defs]** → If a new builtin tool is added to `BUILTIN_TOOL_SCHEMAS` but not to the name set passed to `group_tool_defs_by_server`, it would fall into `unknown`. Mitigation: the set is built from `BUILTIN_TOOL_SCHEMAS.keys() | PSEUDO_TOOL_SCHEMAS.keys()` at call time, so it always matches the schemas that `build_tool_definitions` uses.

- **[OAuth handler registers tools but MCPManager already has them]** → `_register_wrapper` updates MCPManager's `_wrappers` and `_tool_to_server`. The Telegram handler then calls `registry.register_mcp_tools()` which updates the ToolRegistry. These are separate data structures with no overlap. No conflict.

## Migration Plan

No migration needed — this is a bugfix. The three changes are additive:
1. New `builtin_names` parameter defaults to `None` (preserves existing behavior when not passed).
2. OAuth tool registration is 3 new lines after existing success path.
3. Snapshot refresh is a new helper called after existing command logic.

Rollback: revert the three files (`context_monitor.py`, `react_loop.py`, `telegram_commands.py`). No data or config changes.

## Open Questions

- Should `/model` switches also trigger a snapshot refresh? The effective_window and compaction_threshold could change with the model. Out of scope for this change — flagged for future work.
- Should the `context_profile` built-in tool also trigger a refresh? Currently it reads the cached snapshot. If the agent calls `context_profile` after an MCP tool changes something, it would see stale data. Low priority — the agent rarely changes MCP tools mid-run.