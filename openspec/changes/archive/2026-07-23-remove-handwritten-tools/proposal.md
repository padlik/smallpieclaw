# Proposal: remove-handwritten-tools

## Why

The agent has two parallel tool systems: built-in tools (`builtin_executor.py` + `builtin_tools/`) and hand-written tools (`tools/`, `tools_generated/`, `tool_registry.py`, `tool_executor.py`, `tool_creator.py`). The `shell` built-in already subsumes hand-written tools — any `.sh`/`.py` script can be expressed as a `shell` command — while providing superior safety infrastructure (zone-based access control, confirmation gates, lifecycle logging, error classification) that hand-written tools lack.

The `create_tool` action is a security liability: it lets the LLM write executable code to disk (`tools_generated/`) that is then run via subprocess. Even with the dangerous-pattern blocklist in `ToolCreator`, LLM-authored executable code persisted to disk is an attack surface that should not exist. The operator-approved `run` option in the create_tool flow executes arbitrary LLM-provided code via `subprocess.run([sys.executable, "-c", code])` or `bash -c` with only a 30-second timeout and no sandboxing.

MCP tools (registered via `ToolRegistry.register_mcp_tools()`) are the supported extension mechanism for external integrations and are unaffected by this change.

## What Changes

### Removed

- **`tool_executor.py`** — `ToolExecutor` class (subprocess runner for `.sh`/`.py` tools). Deleted entirely.
- **`tool_creator.py`** — `ToolCreator` class (LLM-proposed tool creation with dangerous-pattern blocklist). Deleted entirely.
- **`tools/` directory** — example/placeholder scripts (`check_cpu.sh`, `check_disk.sh`, etc.). Deleted.
- **`tools_generated/` directory** — landing zone for LLM-created tools. Deleted.
- **`create_tool` action** — removed from `react_loop.py` (`_dispatch_create_tool()` function, the `create_tool` action branch, and the native-tool-calling interception), `builtin_tools/schemas.py` (`create_tool` pseudo-tool schema), `confirmation.py` (full tool-creation confirmation subsystem: `request_tool_create()`, `signal_tool_create()`, `get_pending_tool_create()`, state dicts `_tool_create_events`/`_tool_create_results`/`tool_create_pending`, class docstring §3), `agent_controller.py` (`get_pending_tool_create()` and `resume_tool_create()` passthrough methods), `telegram_callbacks.py` (`cb_tool_create` handler and `tool_create_yes/run/no` branches), and `telegram_interface.py` (import, handler registration, `_send_tool_create_prompt()`, and "Create Tool"/"Run Once" buttons).
- **`ToolExecutor` and `ToolCreator` construction** — removed from `main.py`, `agent_controller.py` (constructor params, ReactContext fields, wiring), and `agent_runtime.py` (`executor` and `creator` constructor params at lines 149-150, `self._executor`/`self._creator` attributes, the `executor=controller.executor` / `creator=controller.creator` kwargs in `build_react_context()` at lines 221-222, and the `executor=`/`creator=` kwargs passed to `SubAgentRunner` at lines 370-371).
- **`tools_dir` and `generated_tools_dir` config paths** — removed from `config_schema.py` (`PathsConfig` dataclass fields and parsing).
- **`tools_dir` / `gen_tools_dir` makedirs** — removed from `main.py` startup.

### Modified

- **`tool_registry.py`** — stripped to MCP-only. Removed: `refresh()`, `_parse_tool()`, `register()`, `tools_dirs` constructor param, `_DESC_START_RE`/`_DESC_CONT_RE` regexes. Kept: `Tool` dataclass, `register_mcp_tools()`, `unregister_mcp_server()`, `get()`, `all()`, `exists()`, `summary()`. Constructor takes no arguments and starts with an empty registry.
- **`react_loop.py`** — `ReactContext.executor` and `ReactContext.creator` fields removed. The `return ctx.executor.execute(tool_name, args)` fallback at the end of `_dispatch_tool()` is replaced with an error result for unknown tools.
- **`prompt_builder.py`** — `create_tool` removed from "Possible actions" section. Tool-creation rules removed. "AVAILABLE TOOLS" section kept (now lists only MCP tools via `tool_index.search()`).
- **`prompt_loader.py`** — same prompt template changes as `prompt_builder.py` (if separate templates exist).
- **`builtin_tools/patterns.py`** — the `tools_generated/` dangerous-pattern entry removed (directory no longer exists). Comment referencing `create_tool` gate (lines 26-27) also removed.
- **`builtin_tools/schemas.py`** — module docstring (line 5) updated to remove `create_tool` from pseudo-tools mention.
- **`vulture_whitelist.py`** — entries for removed symbols (`ToolExecutor`, `ToolCreator`, removed `ToolRegistry` methods, `request_tool_create`/`signal_tool_create`/`get_pending_tool_create`) cleaned up.
- **`AGENTS.md`** — module table updated (`tool_executor.py` and `tool_creator.py` removed; `tool_registry.py` description changed to "MCP tool registry"), conventions and testing sections updated, module docstring references to `create_tool` removed.
- **`README.md`** — hand-written tools documentation section removed (including first-run mention of `tools_generated/`, WorkingDirectory note, zone list, project-layout tree references).
- **`config.toml.example`** — `generated_tools_dir` key removed (line ~244).
- **`telegram_callbacks.py`** — `cb_tool_create` handler and `tool_create_yes/run/no` branches removed (moved from Kept — these are part of the `create_tool` confirmation UI).
- **`telegram_interface.py`** — `cb_tool_create` import, handler registration (`^tool_create_` pattern), `_send_tool_create_prompt()`, and "Create Tool"/"Run Once" buttons removed (part of the `create_tool` confirmation UI).

### Kept (no changes)

- `builtin_executor.py` + `builtin_tools/*` — built-in tool logic and dispatch unchanged; `schemas.py` and `patterns.py` receive doc/pattern edits (see Modified).
- `mcp_client.py` — MCP transport (uses `ToolRegistry` for registration, interface unchanged).
- `tool_index.py` — semantic search for built-in + MCP tools (`registry.all()` now returns only MCP tools, `_builtin_tools()` unchanged).
- `telegram_commands.py` — `/tools` lists MCP tools, `/reindex` rebuilds index. The `/tools` empty-state message stays "No tools registered." (no MCP servers configured) — no change needed.
- `scheduler.py` — unaffected (uses `builtin_executor._exec_spawn_agent`).

### Tests & Fixtures

The removal breaks tests and fixtures that reference the removed surface. All must be updated for `make check` to pass:

- **`tests/conftest.py`** — `tmp_agent_dir` fixture creates `tools/` and `tools_generated/` dirs; `minimal_config` fixture seeds `tools_dir`/`generated_tools_dir` config paths. Remove the dir creation and config keys.
- **`tests/execution_harness.py`** — `ReactContext` construction passes `executor=` and `creator=`. Remove those kwargs.
- **Tests passing `executor=MagicMock()` / `creator=MagicMock()` to `ReactContext` or `AgentController`** — at minimum: `test_react_loop.py`, `test_agent_controller_model_restore.py`, `test_context_payload.py`, `test_supervisor_prompt_wiring.py`, `test_prompt_id_logging.py`, `test_running_agent_visibility.py`, `test_graph_memory_integration.py`, `test_p1_non_json_failure.py`, `test_graph_task_outcomes.py`, `test_p3_trace_ids.py`, `test_agent_runtime_skeleton.py`, `test_agent_runtime_characterization.py`, `test_execution_plan.py`, `test_compress_fallback.py`. Remove the kwargs from all constructions.
- **`tests/test_config_schema.py`** — asserts `cfg.paths.tools_dir == "tools"`. Remove the assertion.
- **`tests/test_builtin_executor.py`** — tests `tools_generated/` dangerous-pattern detection. Remove the test.
- **`tests/test_native_intercepts.py`** — tests `create_tool` native interception and `creator=` kwarg. Remove the `create_tool` interception test; keep `plan` and `vision_query` interception tests.
- **`tests/test_tool_index.py`** — constructs `ToolRegistry` with `tools_dirs` param. Update to use the new no-arg constructor.
- **`tests/test_tool_registry.py`** (if exists) — tests `refresh()`, `_parse_tool()`, `register()`. Remove those tests; keep MCP registration tests.
- **`tests/test_access_control.py`** — references `tools/` and `tools_generated/` in zone classification. Update to remove those paths from test expectations.

### Specs Modified

- **`native-tool-calling`** — `create_tool` interception scenario removed; `create_tool` removed from `PSEUDO_TOOL_SCHEMAS` enumeration.
- **`file-access-zones`** — `tools_generated/` removed from the agent-internal directory list.

## Scope

This is a clean removal. No deprecation period, no backward compatibility for existing `tools/` or `tools_generated/` directories. Existing directories on disk are left as orphaned (not cleaned up by the agent) — operators can delete them manually.

The `add-nsjail-shell-isolation` change (in-progress, 0/48 tasks) touches `builtin_tools/shell.py` and `builtin_tools/patterns.py`, which are in the "keep" zone. The only overlap is the `tools_generated/` pattern entry in `patterns.py` — this change removes it, and the nsjail change adds new pattern categories. The two changes are compatible if this one lands first.