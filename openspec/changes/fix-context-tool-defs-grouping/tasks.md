## 1. Fix builtin classification in group_tool_defs_by_server

- [x] 1.1 Add `builtin_names: set[str] | None = None` parameter to `group_tool_defs_by_server` in `context_monitor.py`. When the `ToolRegistry` returns `None` for a tool name, check if the name is in `builtin_names` — if yes, classify as `"builtin"`; if no, classify as `"unknown"`. Default `None` preserves existing behavior.
- [x] 1.2 Update `_tool_defs_by_server_for_context` in `react_loop.py` to build the builtin name set from `BUILTIN_TOOL_SCHEMAS.keys() | PSEUDO_TOOL_SCHEMAS.keys()` (imported from `builtin_tools.schemas`) and pass it as the `builtin_names` argument to `group_tool_defs_by_server`.
- [x] 1.3 Update existing tests in `tests/test_context_monitor.py` to pass `builtin_names` where needed. Add a test case: builtin tool name in `builtin_names` but not in registry → classified as `"builtin"`, not `"unknown"`.

## 2. Register OAuth tools in ToolRegistry after auth success

- [x] 2.1 In `_mcp_auth` (`telegram_commands.py`), after `result.get("success")` is true, call `iface.tool_registry.register_mcp_tools(name, info["tools"])` using `iface.mcp_manager.get_server_info(name)` — mirroring the `/mcp on` handler at line 1144.
- [x] 2.2 Add a test in `tests/test_telegram_command_surface.py` (or a new test file) verifying that after a successful OAuth flow, `register_mcp_tools` is called with the server name and discovered tools.

## 3. Partial snapshot refresh after tool-changing commands

- [x] 3.1 Add a `_refresh_tool_defs_snapshot(iface)` helper in `telegram_commands.py` that: reads the last snapshot from `iface.agent.context_monitor`; if `None`, returns early; builds `builtin_names = BUILTIN_TOOL_SCHEMAS.keys() | PSEUDO_TOOL_SCHEMAS.keys()`; recomputes `tool_defs_by_server` and `tool_defs_tokens` via `group_tool_defs_by_server(build_tool_definitions(iface.mcp_manager), iface.tool_registry, iface.mcp_manager, builtin_names)`; recomputes `danger_level` and `headroom_real` from the new `tool_defs_tokens`; publishes an updated snapshot via `dataclass_replace(last, ..., is_live=False)`. Import `dataclass_replace` from `dataclasses`, `compute_danger_level` and `compute_headroom_real` from `context_monitor`, `build_tool_definitions` and `BUILTIN_TOOL_SCHEMAS` and `PSEUDO_TOOL_SCHEMAS` from `builtin_tools.schemas`.
- [x] 3.2 Call `_refresh_tool_defs_snapshot(iface)` at the end of `_mcp_on` (after the existing `register_mcp_tools` call).
- [x] 3.3 Call `_refresh_tool_defs_snapshot(iface)` at the end of `_mcp_off` (after `unregister_mcp_server`).
- [x] 3.4 Call `_refresh_tool_defs_snapshot(iface)` at the end of `_mcp_auth` success path (after the new `register_mcp_tools` call from task 2.1).
- [x] 3.5 Call `_refresh_tool_defs_snapshot(iface)` at the end of `_mcp_auth_revoke` (after the token file is deleted and tools are unregistered).
- [x] 3.6 Add tests verifying that `_refresh_tool_defs_snapshot` publishes an updated snapshot with correct `tool_defs_by_server`, `tool_defs_tokens`, `danger_level`, and `headroom_real`, and that `is_live` is `False`. Include a test for the no-snapshot case (monitor returns `None` → no publish, no crash).

## 4. Lint and test

- [x] 4.1 Run `ruff check .` and fix any issues.
- [x] 4.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` if new symbols are flagged.
- [x] 4.3 Run `make check` (lint + test) and ensure all tests pass.
- [x] 4.4 Run `openspec validate fix-context-tool-defs-grouping --type change --strict` and fix any validation errors.