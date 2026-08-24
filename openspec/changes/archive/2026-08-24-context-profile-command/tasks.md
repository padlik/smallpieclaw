## 1. ContextMonitor core module

- [x] 1.1 Create `context_monitor.py` with `ContextSnapshot` as an immutable (`frozen=True`) dataclass (fields: system_prompt_tokens, chat_history_tokens, tool_defs_tokens, tool_defs_by_server, completion_reserve, effective_window, compaction_threshold, headroom_nominal, headroom_real, danger_level, is_live, turn)
- [x] 1.2 Implement `ContextMonitor` class with `publish(snapshot: ContextSnapshot) -> None` (reference swap, no lock) and `read() -> ContextSnapshot | None` (returns current snapshot or None)
- [x] 1.3 Implement `compute_danger_level(total_tokens, compaction_threshold) -> str` returning "safe" (below 70%), "approaching" (70% to below 90%), or "danger" (90% or above)
- [x] 1.4 Implement `compute_headroom_real(threshold, system_tokens, history_tokens, tool_defs_tokens) -> int` returning threshold minus system minus history minus tool_defs
- [x] 1.5 Implement `group_tool_defs_by_server(tool_defs, tool_registry, mcp_manager) -> dict[str, int]` that seeds the group dict from the full list of registered MCP servers (from the MCP manager/registry) so empty servers appear with zero, then cross-references tool names from `_tool_defs` back to `ToolRegistry` to get `server_name`/`is_mcp`, groups by "builtin" vs each MCP server name, and estimates tokens per group via `estimate_tokens(json.dumps(group_defs))`. The total `tool_defs_tokens` SHALL be defined as the sum of all per-server group estimates.
- [x] 1.6 Write unit tests for `ContextMonitor` (publish/read, concurrent read during publish, no snapshot before first run, danger level thresholds, headroom computation, server grouping with unknown fallback)

## 2. Wire ContextMonitor into AgentController and ReactContext

- [x] 2.1 Add `context_monitor: ContextMonitor` field to `AgentController.__init__` (construct in `main.py` or default to `ContextMonitor()`)
- [x] 2.2 Add `context_monitor` to `ControllerDeps` dataclass in `agent_runtime.py`
- [x] 2.3 Add `context_monitor` to `ReactContext` dataclass in `react_loop.py`
- [x] 2.4 Wire `context_monitor` through `AgentRuntime.build_react_context()` (copy from `ControllerDeps` to `ReactContext` via `fields()`)
- [x] 2.5 Construct `ContextMonitor` in `main.py` and pass to `AgentController`
- [x] 2.6 Inject `ContextMonitor` into `BuiltinExecutor` at construction time in `main.py` (not via an agent back-reference) so the `context_profile` handler can read the monitor without circular coupling
- [x] 2.7 Update `vulture_whitelist.py` with new public symbols (`ContextMonitor`, `ContextSnapshot`, `context_monitor` field)

## 3. Publish snapshot from ReAct loop

- [x] 3.1 In `react_loop.py`, add a `_publish_context_snapshot(ctx, state, system)` helper that builds a `ContextSnapshot` from current turn data, populating ALL fields: `system_prompt_tokens` via `estimate_tokens(system)`, `chat_history_tokens` via `estimate_messages_tokens(state.messages)`, `tool_defs_by_server` via `group_tool_defs_by_server()`, `tool_defs_tokens` as the sum of per-server groups, `effective_window` and `compaction_threshold` via the shared `resolve_compaction_threshold()` helper (task 4.6), `completion_reserve` from `llm_cfg["max_tokens"]`, `headroom_nominal` and `headroom_real` via the compute helpers, `danger_level` via `compute_danger_level()`, `is_live=True`, `turn=state.step`
- [x] 3.2 Call `_publish_context_snapshot` after each turn completes in the ReAct loop (after the LLM response is processed, before the next iteration)
- [x] 3.3 Set `is_live=True` and `turn=state.step` in the snapshot during a run
- [x] 3.4 When `react_loop` finishes, publish a new `ContextSnapshot` with `is_live=False` (copy the last snapshot's field values but with `is_live=False`) so `/context` works between runs. Never mutate the existing snapshot — always publish a new immutable one.
- [x] 3.5 Write tests for snapshot publication (verify snapshot fields after a turn, verify is_live flag, verify last snapshot retained after run ends)

## 4. Fix maybe_compact tool-def invisibility

- [x] 4.1 Add `tool_defs_tokens: int = 0` parameter to `maybe_compact()` in `context_manager.py`
- [x] 4.2 Update the total computation: `total = estimate_messages_tokens(messages, system, model) + tool_defs_tokens`
- [x] 4.3 Update the `react_loop.py` call site to compute `tool_defs_tokens` as the sum of per-server group estimates from `group_tool_defs_by_server()` (the same value used in the snapshot) and pass it to `maybe_compact()`. This ensures the compaction total and the displayed total are the same number.
- [x] 4.4 Write tests for `maybe_compact` with `tool_defs_tokens > 0` (compaction triggers when system+history+tool_defs exceeds threshold, even if system+history alone is under threshold)
- [x] 4.5 Write test verifying backward compatibility: `maybe_compact()` without `tool_defs_tokens` behaves identically to pre-change (default 0)
- [x] 4.6 Extract a shared `resolve_compaction_threshold(llm_cfg, ctx_max_tokens) -> tuple[int, int]` helper (returns `(effective_window, compaction_threshold)`) that both `maybe_compact()` and `_publish_context_snapshot()` call, ensuring the threshold computation is identical in both paths. The formula: `effective = llm_cfg.get("context_window") or ctx_max_tokens`, `threshold = max(int((effective - llm_cfg.get("max_tokens", 1024)) * 0.85), 256)`.

## 5. context_profile built-in tool

- [x] 5.1 Add `context_profile` to `BUILTIN_TOOLS` in `builtin_tools/descriptors.py` with a prose description (name, description, language)
- [x] 5.2 Add `context_profile` JSON schema to `BUILTIN_TOOL_SCHEMAS` in `builtin_tools/schemas.py` (no parameters, returns JSON object)
- [x] 5.3 Create handler for `context_profile` in `builtin_tools/` (new module or addition to existing module like `context_io.py`) that reads from `ContextMonitor` injected directly into the `BuiltinExecutor` at construction time (not via an agent back-reference) and returns a JSON snapshot
- [x] 5.4 Register the handler in `builtin_executor.py` dispatch
- [x] 5.5 Verify `build_tool_definitions()` includes `context_profile` in the tool definitions array
- [x] 5.6 Write tests for `context_profile` tool (returns JSON with expected fields, success=True, not confirmation-capable, enumerated in `is_builtin`/`all_tools`)

## 6. /context Telegram command

- [x] 6.1 Implement `cmd_context(iface, update, ctx)` in `telegram_commands.py` with `@_require_auth` — reads `iface.agent.context_monitor.read()`, renders summary dashboard
- [x] 6.2 Implement the dashboard rendering: model name, effective window, live indicator, token counts + percentages + bar chart for each category (system, history, tool defs, completion), danger level, real headroom, tool defs grouped by MCP server
- [x] 6.3 Handle no-snapshot case (agent hasn't run yet) with a clear message
- [x] 6.4 Import `cmd_context` in `telegram_interface.py` (add to the `from telegram_commands import (...)` block)
- [x] 6.5 Register `CommandHandler("context", partial(cmd_context, self))` in `_register_handlers()`
- [x] 6.6 Add `BotCommand("context", "Show context window consumption profile")` to `_post_init()` BotFather menu
- [x] 6.7 Add `/context` to the help text in `cmd_help`
- [x] 6.8 Write tests for `cmd_context` (dashboard rendering with live snapshot, with idle snapshot, with no snapshot)

## 7. Documentation and config

- [x] 7.1 Update README with `/context` command description and `context_profile` tool description
- [x] 7.2 Update config example (`config.toml.example`) if any new config fields are needed (none expected — monitor is always on)
- [x] 7.3 Update `AGENTS.md` module table with `context_monitor.py` entry
- [x] 7.4 Update `vulture_whitelist.py` with any new public API symbols flagged by vulture

## 8. Validation and lint

- [x] 8.1 Run `ruff check .` and fix any issues
- [x] 8.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and fix any issues
- [x] 8.3 Run `make test` and ensure all tests pass (existing 1627 + new tests)
- [x] 8.4 Run `openspec validate context-profile-command --type change --strict` to validate spec coherence