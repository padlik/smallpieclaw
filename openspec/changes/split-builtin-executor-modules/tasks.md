## 1. Phase 0 — Extract stateless leaves + re-exports

- [x] 1.1 Create `builtin_tools/` package with a light/empty `__init__.py` (must NOT eagerly import handler modules, to avoid import cycles per design Risks).
- [x] 1.2 Move `BuiltinTool` dataclass + `BUILTIN_TOOLS` dict (incl. the `vision_query` descriptor) into `builtin_tools/descriptors.py`.
- [x] 1.3 Move `_DANGEROUS_SHELL_PATTERNS`, `_SENSITIVE_PATH_PATTERNS`, `_is_dangerous_shell`, `_is_sensitive_path` into `builtin_tools/patterns.py`.
- [x] 1.4 Move `_truncate_output`, `_truncate_tail` into `builtin_tools/text_utils.py`.
- [x] 1.5 Move log-query consts + `_log_level_to_num`, `_log_query_default_keep`, `_read_tail_lines`, `_log_query_project` into `builtin_tools/logquery_helpers.py`.
- [x] 1.6 Move `_validate_context_key`, `_context_path`, `_save_context`, `_load_context` into `builtin_tools/context_io.py` (keep `memory_store` import function-local).
- [x] 1.7 In `builtin_executor.py`, re-export the moved symbols (`BuiltinTool`, `BUILTIN_TOOLS`, `_is_dangerous_shell`, `_is_sensitive_path`, `_truncate_output`, `_truncate_tail`, `_validate_context_key`, `_load_context`, `_save_context`) so existing value-imports keep resolving.
- [x] 1.8 Run `make check` and `python -c "import main"` + `python -c "import builtin_executor"`; update `vulture_whitelist.py` for any newly public leaf symbols flagged ≥80.

## 2. Phase 1 — Dispatch registry (no bodies move)

- [x] 2.1 Introduce `_exec_table` (14 entries, all tools except `vision_query`) mapping name → per-tool adapter that reproduces each tool's exact current kwargs (per design Decision 3; `chunk_callback` threaded for `shell`).
- [x] 2.2 Introduce `_run_table` with exactly the 6 confirmation tools (`shell`, `file_read`, `file_write`, `file_patch`, `memory_graph_store`, `secret_get`).
- [x] 2.3 Rewrite `_dispatch` and `_run` to look up the tables (unknown name → the existing error dict, never raise); keep `_exec_spawn_agent`/`_exec_get_agent_result`/`_exec_schedule` as real methods with verbatim signatures (no keyword-only `*,`).
- [x] 2.4 Run `make check` + import smoke; confirm no behavior change (dispatch is the only change this phase).

## 3. Phase 2 — FileTools (files.py)

- [x] 3.1 Create `builtin_tools/files.py` with `FileTools(owner)`; move `_exec_file_read`/`_run_file_read`, `_exec_file_write`/`_run_file_write`, `_exec_file_patch`/`_run_file_patch`, `_exec_file_diff`, `_exec_file_send` bodies; read `max_output` and confirmation via `owner`.
- [x] 3.2 Construct `self._files = FileTools(self)` in `__init__`; repoint the relevant `_exec_table`/`_run_table` entries (incl. `file_read` in `_run_table`).
- [x] 3.3 Run `make check` (esp. `tests/test_builtin_executor.py`) + import smoke; update `vulture_whitelist.py` as needed.

## 4. Phase 3 — MemoryTools (memory.py)

- [x] 4.1 Create `builtin_tools/memory.py` with `MemoryTools(owner)`; move `_exec_memory_write`, `_exec_memory_graph_search`, `_exec_memory_graph_store`/`_run_memory_graph_store`; read `_memory`/`_graph_memory`/`_graph_memory_writer` via `owner` at call time; keep `graph_memory` import function-local.
- [x] 4.2 Wire `self._memory_tools = MemoryTools(self)`; repoint table entries (`memory_graph_store` in both `_exec_table` and `_run_table`; `memory_write`/`memory_graph_search` in `_exec_table`).
- [x] 4.3 Run `make check` (`tests/test_graph_memory_integration.py`, `tests/test_p2_graph_memory_admission.py`, `tests/test_p2_longterm_consolidation.py`) + import smoke; update whitelist.

## 5. Phase 4 — SecretsTools + LogQueryTools (secrets_log.py)

- [x] 5.1a Create `builtin_tools/secrets_log.py` with `SecretsTools(owner)` (`_exec_secret_get`/`_run_secret_get`); read `_vault_path` via `owner`; keep `config_schema` import function-local; secret_get headless path calls `owner`'s bridge.
- [x] 5.1b Add `LogQueryTools(owner)` (`_exec_log_query`) to `builtin_tools/secrets_log.py`; read `_log_jsonl_path`/`max_output` via `owner`.
- [x] 5.2 Wire both handlers; repoint table entries (`secret_get` in both `_exec_table` and `_run_table`; `log_query` in `_exec_table`).
- [x] 5.3 Run `make check` (`tests/test_log_query.py`, vault tests) + import smoke (`python -c "import main"` and `import builtin_executor`); update whitelist.

## 6. Phase 5 — ShellTools (shell.py)

- [x] 6.1a Create `builtin_tools/shell.py` with `ShellTools(owner)` and move the shell log helpers `_open_shell_log`, `_finalize_shell_log`; read `_data_dir` via `owner`.
- [x] 6.1b Move the shell core `_exec_shell`, `_run_shell`, `_run_shell_subprocess`, `_run_shell_pty` into `ShellTools`; read `default_timeout`/`max_output`/`_shell_*` via `owner`; preserve PTY fallback, process-group kill, and incremental UTF-8 decode exactly.
- [x] 6.2 Wire `self._shell = ShellTools(self)`; repoint `shell` in both `_exec_table` and `_run_table` (thread `chunk_callback`).
- [x] 6.3 Run `make check` (`tests/test_builtin_executor.py`) + import smoke (`python -c "import main"` and `import builtin_executor`); update whitelist.

## 7. Phase 6 — AgentTools (agents.py)

- [x] 7.1 Create `builtin_tools/agents.py` with `AgentTools(owner)`; move `_exec_spawn_agent`, `_exec_get_agent_result` bodies; read `_sub_agent_factory`/`_working`/`_results`/`_graph_memory`/`_notify_html_fn`/`_supervisor`/`_max_subagents`/`_subagent_result_timeout`/`_data_dir` via `owner` at call time; keep `agent_runtime`/`sub_agent_registry`/`prompt_loader` imports function-local; keep supervisor delegation (ADR-0005).
- [x] 7.2 Keep `_exec_spawn_agent`/`_exec_get_agent_result` as real façade forwarders (verbatim signatures) delegating to `self._agents.*`, since `scheduler.py:683` and tests call them directly and assert on `call_args`. Keep `_exec_schedule` inline on the façade.
- [x] 7.3 Update the monkeypatch in `tests/test_subagent_context_persistence.py`: repoint `patch("builtin_executor._save_context")` to the module the caller resolves (`builtin_tools.context_io._save_context`) and assert the spy fires.
- [x] 7.4 Run `make check` (`tests/test_spawn_agent.py`, `tests/test_context_payload.py`, `tests/test_sub_agent_supervisor.py`, `tests/test_scheduler_fallback.py`, `tests/test_p1_subagent_confirm.py`, `tests/test_p2_longterm_consolidation.py`, `tests/test_subagent_context_persistence.py`) + import smoke (`python -c "import main"` and `import builtin_executor`); update whitelist.

## 8. Finalization & verification

- [x] 8.1 Add a routing test asserting: `set(BUILTIN_TOOLS)` equals the frozen 15-tool set; every non-`vision_query` name resolves in `_exec_table`; `_run_table` keyset equals exactly the 6 confirmation tools; `is_builtin("vision_query")` is True while `vision_query` is absent from both tables; and `execute` with an unknown tool name returns `success=False` with a non-empty `error` and raises no exception. (Consider adding this test in Phase 1 so it guards table drift across phases 2–6.)
- [x] 8.2 Re-grep the repo for any remaining `patch("builtin_executor.<symbol>")` by module path (beyond `_save_context`) and repoint as needed.
- [x] 8.3 Confirm `builtin_executor.py` is reduced to façade + registries + confirmation state + re-exports (+ inline `_exec_schedule`), and no handler snapshots the 8 late-bound settables.
- [x] 8.4 Verify the three seam constraints (Decision 8 / ADR-0008): grep `builtin_tools/*.py` handler modules for (a) no `_pending`/`_headless_confirm_*` references — handlers touch confirmation only via `_requires_confirmation`/`confirm`/`cancel`; (b) no `agent_logging.log_event` / `TOOL_START`/`TOOL_END`/`TOOL_FAILED` emissions (lifecycle logging stays on the façade, module-level); (c) `_run_table` is the sole phase-2 dispatch point.
- [x] 8.5 Run full `make check` (ruff + vulture + pytest) green; run `python -c "import main"` once more.
- [x] 8.6 Run `openspec validate split-builtin-executor-modules --type change --strict` before archive.
