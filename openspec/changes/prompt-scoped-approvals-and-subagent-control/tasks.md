## 1. Prompt Registry

- [x] 1.1 Create `prompt_registry.py` with `PromptRecord` dataclass (`prompt_id`, `trace_id`, `text`, `started_at`, `ended_at`, `status`, `sub_agent_ids`) and `PromptRegistry` class (`start`, `finish`, `add_sub_agent`, `get`, `by_trace`, `list_recent`)
- [x] 1.2 Implement `data/prompts.jsonl` append-only persistence in `PromptRegistry`: `start()` appends a record, `add_sub_agent()` appends an update line, `finish()` appends a finalization line with full `sub_agent_ids`
- [x] 1.3 Implement startup reload: `PromptRegistry` replays `data/prompts.jsonl` on init (last-line-wins for mutable fields), recovers next `prompt_id` = max existing + 1
- [x] 1.4 Construct `PromptRegistry` singleton in `main.py`, wire it onto `BuiltinExecutor._prompt_registry` (long-lived reference)
- [x] 1.5 Call `PromptRegistry.start(trace_id, text)` in `TelegramInterface._run_agent_task_locked` (`telegram_interface.py:444`) before `run_in_executor`; thread the returned `prompt_id` into `AgentController.run()` (add `prompt_id` parameter to `run()`); call `PromptRegistry.finish(prompt_id, status)` in the `finally` block (`telegram_interface.py:610`)
- [x] 1.6 Write tests for `PromptRegistry`: ID assignment, persistence, reload across "restarts", `add_sub_agent` update lines, `list_recent` ordering, `start`/`finish` invoked from `_run_agent_task_locked`

## 2. Per-Prompt Approval TTL

- [x] 2.1 Add `self._confirmation.clear_auto_approve()` to `AgentController.run()`'s finally block (`agent_controller.py:181`)
- [x] 2.2 Add `_prompt_approval_set: Optional[set[str]]` field to `BuiltinExecutor` (`builtin_executor.py`), initialized to `None`
- [x] 2.3 Wire `executor._prompt_approval_set = self._confirmation.auto_approve_tools` at `AgentController.run()` start; set to `None` in finally
- [x] 2.4 Add approve-all check to `_headless_confirm_bridge` (`builtin_executor.py:453`): if `self._prompt_approval_set is not None and tool_name in self._prompt_approval_set`, auto-confirm via `confirm(token)` without prompting
- [x] 2.5 Write tests: grant expires at run end, sub-agent auto-approves when tool in shared set, sub-agent prompts when tool not in set, fail-closed when set is `None` after run end

## 3. Sub-Agent Approve-All Button

- [x] 3.1 Add "Approve all `<tool>`" button to `send_subagent_confirmation_prompt` (`telegram_interface.py:919`) with callback data `subconfirm_all:{token}:{tool_name}` — restrict to file tools only (`file_read`, `file_write`, `file_patch`); never render for `shell`
- [x] 3.2 Extend `cb_subagent_confirm` (`telegram_callbacks.py:348`) to handle `subconfirm_all`: add `tool_name` to `iface.agent._confirmation.auto_approve_tools`, then call `builtin.signal_headless_confirm(token, True)`
- [x] 3.3 Write tests: button renders for file tools, button does not render for `shell`, tapping adds to shared set and confirms current op, subsequent sub-agent ops for that tool auto-approve, `"shell"` can never enter `auto_approve_tools`

## 4. wait_for_any_agent Tool

- [x] 4.1 Implement `_exec_wait_for_any_agent` in `builtin_tools/agents.py`: poll loop (200ms sleep) checking `record._result_event.is_set()` and `record.status in ("done","failed","cancelled")` for each `agent_id`; return first completed; timeout returns `{status:"timeout"}`; unknown agent_id returns error
- [x] 4.2 Add `wait_for_any_agent` descriptor to `builtin_tools/descriptors.py` and schema to `builtin_tools/schemas.py`
- [x] 4.3 Add routing entry to `builtin_executor.py` `_exec_table` and `_run_table`
- [x] 4.4 Write tests: first completed returned, already-finished returns immediately, failed/cancelled returned as completed, timeout returns no result, unknown agent_id rejected

## 5. cancel_agent Tool

- [x] 5.1 Implement `_exec_cancel_agent` in `builtin_tools/agents.py`: if `agent_id` in ("managed","all") call `get_registry().cancel_all_managed()`; else call `get_registry().cancel(agent_id)`; return `{success, output}`
- [x] 5.2 Add `cancel_agent` descriptor to `builtin_tools/descriptors.py` and schema to `builtin_tools/schemas.py`
- [x] 5.3 Add routing entry to `builtin_executor.py` `_exec_table` and `_run_table`
- [x] 5.4 Write tests: cancel specific agent, cancel all managed, not confirmation-gated, unknown agent_id handled

## 6. /stop Cascade

- [x] 6.1 Add `get_registry().cancel_all_managed()` call to `cmd_stop` (`telegram_commands.py:247`) after `iface.agent.cancel()`
- [x] 6.2 Update the `/stop` confirmation message to indicate main agent and all sub-agents are cancelling
- [x] 6.3 Write tests: `/stop` cancels on-demand sub-agents, `/stop` cancels scheduled sub-agents, plan-step sub-agents cancelled via existing bridge

## 7. /prompts Command

- [x] 7.1 Implement `cmd_prompts` in `telegram_commands.py`: call `PromptRegistry.list_recent(20)`, format each entry (ID, status, elapsed, sub-agent count), reply with HTML
- [x] 7.2 Register `/prompts` handler in `telegram_interface.py` and add to the bot command list
- [x] 7.3 Add `/prompts` to help text
- [x] 7.4 Write tests: `/prompts` lists recent prompts, empty registry shows empty list

## 8. Prompt ID in Logs

- [x] 8.1 Add `prompt_id` parameter to `bind_run_context()` in `agent_logging.py` and bind it into the structlog context alongside `trace`/`agent`/run-label
- [x] 8.2 Call `bind_run_context(prompt_id=...)` at `AgentController.run()` start (after `PromptRegistry.start()`)
- [x] 8.3 Call `bind_run_context(prompt_id=...)` in `SubAgentSupervisor._run_and_notify` (`sub_agent_supervisor.py:147`) before `runner.run(task)` so sub-agent logs inherit the parent prompt_id
- [x] 8.4 Write tests: main agent log lines carry `prompt_id`, sub-agent log lines carry parent `prompt_id`, startup logs have no `prompt_id` (degrades gracefully)

## 9. log_query prompt_id Filter

- [x] 9.1 Add `prompt_id` optional parameter to the `log_query` tool schema in `builtin_tools/schemas.py`
- [x] 9.2 Implement `prompt_id` filtering in `_exec_log_query` (`builtin_tools/secrets_log.py`): filter records by the `prompt_id` field directly from `agent.jsonl` (no registry join)
- [x] 9.3 Write tests: filter by prompt_id returns only matching records, prompt_id filter combines with trace/level/event filters, empty result is well-formed

## 10. Supervisor Prompt Registry Wiring

- [x] 10.1 Add `_current_prompt_id: Optional[int]` and `_prompt_registry: Optional[PromptRegistry]` fields to `BuiltinExecutor` (the `_prompt_registry` wiring in `main.py` is owned by task 1.4)
- [x] 10.2 Set `executor._current_prompt_id` at `AgentController.run()` start; clear in finally
- [x] 10.3 In `SubAgentSupervisor.submit` (`sub_agent_supervisor.py:128`), after `register_run`, call `owner._prompt_registry.add_sub_agent(owner._current_prompt_id, runner.agent_id)` if both are non-None
- [x] 10.4 Write tests: spawned sub-agent recorded against active prompt, no recording when `_current_prompt_id` is None (e.g. scheduled runs without a prompt context)

## 11. Prompt Builder Guidance

- [x] 11.1 Add prompt guidance to `prompt_builder.py` for `wait_for_any_agent` (call repeatedly to collect results in completion order, decide after each), `cancel_agent` (stop a sub-agent you no longer need), and the per-prompt approval model (grants expire when you present your final answer)
- [x] 11.2 Update the built-in tools list in the prompt to include `wait_for_any_agent` and `cancel_agent`
- [x] 11.3 Verify the prompt renders without errors and token estimate is reasonable

## 12. Built-in Tool Count Update

- [x] 12.1 Update the built-in tool count from 15 to 17 in `builtin_executor.py` and any tests that assert the count
- [x] 12.2 Verify `is_builtin` and `all_tools` enumerate 17 tools including the two new ones
- [x] 12.3 Verify the confirmation-capable set stays at 6 (the new tools are not in it)

## 13. Validation and Lint

- [x] 13.1 Run `openspec validate prompt-scoped-approvals-and-subagent-control --type change --strict` and fix any issues
- [x] 13.2 Run `ruff check .` and fix any lint errors
- [x] 13.3 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` for any new public API symbols
- [x] 13.4 Run `make test` and ensure all tests pass