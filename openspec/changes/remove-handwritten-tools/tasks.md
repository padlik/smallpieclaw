# Tasks: remove-handwritten-tools

## 1. Delete hand-written tool files and directories

- [x] 1.1 Delete `tool_executor.py`
- [x] 1.2 Delete `tool_creator.py`
- [x] 1.3 Delete `tools/` directory (check_cpu.sh, check_disk.sh, check_memory.sh, check_network.sh, check_logs.sh, docker_status.sh, system_health.py, temperature.sh, example_tool.sh)
- [x] 1.4 Delete `tools_generated/` directory (if it exists; may be empty or contain LLM-created tools from prior runs)

## 2. Strip ToolRegistry to MCP-only

- [x] 2.1 Remove `refresh()`, `_parse_tool()`, `register()` methods from `tool_registry.py`
- [x] 2.2 Remove `_DESC_START_RE`, `_DESC_CONT_RE` regex constants and `os`/`re` imports if now unused
- [x] 2.3 Change `__init__` to take no arguments: `def __init__(self)` — empty `_registry` dict, no `refresh()` call, no `tools_dirs` param
- [x] 2.4 Keep `Tool` dataclass, `register_mcp_tools()`, `unregister_mcp_server()`, `get()`, `all()`, `exists()`, `summary()` unchanged
- [x] 2.5 Update module docstring to reflect MCP-only role

## 3. Remove create_tool action from react_loop.py

- [x] 3.1 Delete `_dispatch_create_tool()` function (lines ~1367-1426)
- [x] 3.2 Delete the `create_tool` action branch in the main action dispatch (lines ~838-842)
- [x] 3.3 Remove `create_tool` from the native-tool-calling interception (keep `plan` and `vision_query`); remove stale intercept comment at line 461
- [x] 3.4 Remove `ReactContext.executor` and `ReactContext.creator` dataclass fields
- [x] 3.5 Replace the `return ctx.executor.execute(tool_name, args)` fallback in `_dispatch_tool()` with an error result: `{"success": False, "output": "", "error": f"Tool '{tool_name}' is not a built-in tool, MCP tool, or vision_query.", "exit_code": -1}`
- [x] 3.6 Update the "Unknown action" message at line 863: change `Use "tool", "create_tool", or "finish"` to `Use "tool", "plan", or "finish"`

## 4. Remove create_tool confirmation subsystem

- [x] 4.1 Remove `create_tool` entry from `PSEUDO_TOOL_SCHEMAS` in `builtin_tools/schemas.py` (keep `plan`). Update module docstring (line 5) to remove `create_tool` from pseudo-tools mention.
- [x] 4.2 Remove full tool-creation subsystem from `confirmation.py`: `request_tool_create()`, `signal_tool_create()`, `get_pending_tool_create()` methods; state dicts `_tool_create_events`, `_tool_create_results`, `tool_create_pending`; class docstring §3 ("Tool creation")
- [x] 4.3 Remove `get_pending_tool_create()` and `resume_tool_create()` passthrough methods from `agent_controller.py` (lines 237-243)
- [x] 4.4 Remove `cb_tool_create` handler and `tool_create_yes/run/no` branches from `telegram_callbacks.py` (line 136+)
- [x] 4.5 Remove from `telegram_interface.py`: `cb_tool_create` import (line 51), handler registration `^tool_create_` pattern (line 266), `__TOOL_CREATE__:` dispatch branch (lines 565-571), `_send_tool_create_prompt()` method (lines 742+), and "Create Tool"/"Run Once" buttons (lines 763-765)

## 5. Remove ToolExecutor and ToolCreator wiring from main.py

- [x] 5.1 Remove imports: `from tool_creator import ToolCreator` and `from tool_executor import ToolExecutor` (lines 116-117)
- [x] 5.2 Remove `ToolExecutor` construction (line 269) and `ToolCreator` construction (line 270)
- [x] 5.3 Remove `executor=executor` and `creator=creator` kwargs from `AgentController` construction (lines 325-326)
- [x] 5.4 Remove `os.makedirs(tools_dir, exist_ok=True)` (line 219) and `os.makedirs(gen_tools_dir, exist_ok=True)` (line 220)
- [x] 5.5 Remove `tools_dir` and `gen_tools_dir` variable assignments from `main()` (lines 167-168) and `_run()` params

## 6. Remove executor/creator from agent_controller.py

- [x] 6.1 Remove `executor` and `creator` params from `AgentController.__init__()`
- [x] 6.2 Remove `self.executor` and `self.creator` attribute assignments
- [x] 6.3 Remove `executor=self.executor` and `creator=self.creator` from `ReactContext` construction in `run()`
- [x] 6.4 Remove imports `from tool_creator import ToolCreator` and `from tool_executor import ToolExecutor` (lines 41-42)
- [x] 6.5 Update module docstring at line 10 ("Dispatch action: tool | create_tool | finish" → "tool | plan | finish")
- [x] 6.6 Update SubAgentRunner docstring at line ~444 (references `ToolExecutor`, `ToolCreator`)

## 7. Remove executor/creator from agent_runtime.py

- [x] 7.1 Remove `executor` and `creator` params from `AgentRuntime.__init__()` (lines 149-150)
- [x] 7.2 Remove `self._executor` and `self._creator` attribute assignments
- [x] 7.3 Remove `executor=controller.executor` and `creator=controller.creator` from `build_react_context()` (lines 221-222)
- [x] 7.4 Remove `executor=self._executor` and `creator=self._creator` from `SubAgentRunner` construction (lines 370-371)

## 8. Remove config paths

- [x] 8.1 Remove `tools_dir` and `generated_tools_dir` fields from `PathsConfig` dataclass in `config_schema.py`
- [x] 8.2 Remove the corresponding parsing lines in `config_schema.py` (the `_expand_path(section.get(...))` calls)

## 9. Update prompt templates

- [x] 9.1 In `prompts/system/05-response-format.md`: remove `create_tool` action definition (line 27) from "Possible actions" section, leaving `tool` and `finish`
- [x] 9.2 In `prompts/system/04-execution.md`: remove the full tool-creation content from the `TOOL CREATION AND EXECUTION RULES:` block — remove the header (line 29), line 30 ("Always try shell / file_read / file_write before proposing a new tool"), and lines 34-40 (tool-creation rules: "Use the shell tool for one-off...", "Propose a new tool ONLY when...", "Tools must follow the UNIX paradigm...", "Prefer Python for tools...", "Never hardcode paths...", "It is fine to propose multiple small tools...", "All tool creation requires operator confirmation..."). Rename header to `EXECUTION RULES:`. Keep lines 31-33 (skill usage rules) and lines 41-43 (dangerous commands ban, retry guidance, finish action).
- [x] 9.3 In `prompts/system/03-capabilities.md`: remove "prefer these before creating new tools" phrase (line 14)
- [x] 9.4 In `prompts/sub-agent/04-response-format.md`: remove `create_tool` from "Possible actions" list (line 14) if present
- [x] 9.5 In `prompt_builder.py`: remove `create_tool` from the inline "Possible actions" template (line ~173)

## 10. Remove tools_generated/ pattern

- [x] 10.1 Remove the `tools_generated/` dangerous-pattern entry from `builtin_tools/patterns.py` (line ~28) and the comment referencing `create_tool` gate (lines 26-27)

## 11. Update vulture_whitelist.py

- [x] 11.1 Remove whitelist entries for `ToolExecutor`, `ToolCreator`, removed `ToolRegistry` methods (`refresh`, `_parse_tool`, `register`), and `request_tool_create`/`signal_tool_create`/`get_pending_tool_create`

## 12. Update specs

- [x] 12.1 Sync `native-tool-calling` delta spec to main spec (MODIFIED: Special-case tool interception, Tool definition assembly)
- [x] 12.2 Sync `file-access-zones` delta spec to main spec (MODIFIED: Paths are classified into zones — remove `tools/` and `tools_generated/` from agent-internal directory list)

## 13. Update documentation

- [x] 13.1 Update `AGENTS.md` module table: remove `tool_executor.py` and `tool_creator.py` rows; change `tool_registry.py` description to "MCP tool registry"; update testing fixtures section (remove `tools/` and `tools_generated/` from `tmp_agent_dir` description); remove `create_tool` from module docstring references
- [x] 13.2 Update `README.md`: remove all hand-written tools references — first-run mention of `tools_generated/` (L125), WorkingDirectory note (L154), zone list (L230), "Create .py or .sh files in tools/" section (L252+), project-layout tree (L505, L522-523)
- [x] 13.3 Remove `generated_tools_dir` key from `config.toml.example` (line ~244)

## 14. Update tests and fixtures

- [x] 14.1 Update `tests/conftest.py`: remove `tools/` and `tools_generated/` dir creation from `tmp_agent_dir` fixture; remove `tools_dir`/`generated_tools_dir` keys from `minimal_config` fixture
- [x] 14.2 Update `tests/execution_harness.py`: remove `executor=` and `creator=` kwargs from `run_react()` and `_NullToolIndex` if it references them
- [x] 14.3 Remove `executor=MagicMock()` and `creator=MagicMock()` kwargs from all test constructions of `ReactContext` and `AgentController` (at minimum: test_react_loop.py, test_agent_controller_model_restore.py, test_context_payload.py, test_supervisor_prompt_wiring.py, test_prompt_id_logging.py, test_running_agent_visibility.py, test_graph_memory_integration.py, test_p1_non_json_failure.py, test_graph_task_outcomes.py, test_p3_trace_ids.py, test_agent_runtime_skeleton.py, test_execution_plan.py, test_compress_fallback.py)
- [x] 14.4 In `tests/test_agent_runtime_characterization.py`: delete assertions at lines 140-141 (`assert ctx.executor is ctrl.executor` / `assert ctx.creator is ctrl.creator`) — these will AttributeError after field removal
- [x] 14.5 Update `tests/test_config_schema.py`: remove assertion `cfg.paths.tools_dir == "tools"` and any `generated_tools_dir` assertions
- [x] 14.6 Remove `tools_generated/` dangerous-pattern test from `tests/test_builtin_executor.py` (line ~89-90)
- [x] 14.7 Remove `create_tool` interception test from `tests/test_native_intercepts.py` (keep `plan` and `vision_query` interception tests)
- [x] 14.8 Update `tests/test_tool_index.py`: change `ToolRegistry` construction from `tools_dirs=[...]` to no-arg `ToolRegistry()`
- [x] 14.9 Update `tests/test_tool_registry.py` (if exists): remove tests for `refresh()`, `_parse_tool()`, `register()`; keep MCP registration tests
- [x] 14.10 Update `tests/test_access_control.py`: remove `tools/` and `tools_generated/` from zone classification test expectations; remove `generated_tools_dir=` constructor kwarg (line ~23); remove `paths.generated_tools_dir` access (line ~37) and `paths.tools_dir`/`paths.generated_tools_dir` list access (line ~169)
- [x] 14.11 Update `tests/test_file_tools_zone.py`: remove `tools/` from agent-internal path test expectations (line ~97)

## 15. Verify

- [x] 15.1 Run `make lint` (ruff check + vulture) — fix any issues
- [x] 15.2 Run `make test` — all tests pass
- [x] 15.3 Run `make check` (lint + test) — clean pass