## 1. Checkpoint Store Module

- [ ] 1.1 Create `checkpoint_store.py` with `CheckpointStore` class: `save(trace_id, state_dict)`, `load(trace_id)`, `delete(trace_id)`, `list()` methods. Atomic write (tmp → `os.replace`). `save()` catches `OSError` non-fatally (logs warning, returns without raising). `load()` catches `json.JSONDecodeError` and `KeyError` (returns None for corrupted files). `list()` returns checkpoints sorted by `created_at` descending.
- [ ] 1.2 Add `data/run_checkpoints/` directory creation in `CheckpointStore.__init__` (mkdir, exist_ok=True). Path resolved relative to the agent's data directory per ADR-0019.
- [ ] 1.3 Write unit tests for `CheckpointStore`: save/load round-trip, atomic write (verify .tmp not left behind), delete, list sorting, corrupted file handling, OSError non-fatal, missing directory auto-creation.

## 2. Error Classification

- [ ] 2.1 Add `_classify_llm_error(exc) -> LLMErrorInfo` function in `react_loop.py`. `LLMErrorInfo` is a dataclass with fields: `type` (str), `message` (str), `retryable` (bool), `detail` (str). Classification logic: `httpx.TimeoutException` → timeout, `httpx.ConnectError` → connection, `httpx.HTTPStatusError` with 429 → rate_limit, `LLMEmptyResponseError` → empty, `LLMContextOverflowError` → context (non-retryable), `LLMPermanentError` → permanent (non-retryable), any other exception → unknown (retryable). `LLMCancelledError` is NOT classified here — it propagates before classification.
- [ ] 2.2 Write unit tests for `_classify_llm_error`: one test per error type (7 types + LLMCancelledError propagation test). Verify type, message prefix, retryable flag, and detail extraction.

## 3. Confirmation Manager Retry Support

- [ ] 3.1 Add `request_retry(token, error_info_json, progress_cb, timeout_seconds) -> str` method to `ConfirmationManager`. Blocks on `threading.Event` with configurable timeout. Sends `__LLM_ERROR__:{token}:{json}` to `progress_cb`. Returns "retry", "cancel", or "timeout".
- [ ] 3.2 Add `signal_retry(token, response)` method to `ConfirmationManager`. Sets the event and stores the response. Same pattern as `signal_confirmation` / `signal_extension`.
- [ ] 3.3 Add `RETRY_PREFIX = "__LLM_ERROR__"` constant to `confirmation.py`.
- [ ] 3.4 Add `_retry_events` and `_retry_results` dicts to `ConfirmationManager.__init__`.
- [ ] 3.5 Write unit tests for `request_retry` / `signal_retry`: retry response, cancel response, timeout response, signal after timeout (no-op).

## 4. React Loop Error Handling

- [ ] 4.1 Modify `_request_turn()` in `react_loop.py`: replace the broad `except Exception as exc` in the json_mode fallback path with specific exception handling. Catch `LLMCancelledError` first (re-raise immediately). Then catch all other exceptions → call `_classify_llm_error(exc)` → call `_handle_llm_error(ctx, state, error_info, progress)`.
- [ ] 4.2 Add `_handle_llm_error(ctx, state, error_info, progress) -> Optional[str]` function in `react_loop.py`. Steps: (1) write checkpoint via `ctx.checkpoint_store.save()` if `checkpoint_enabled`, (2) call `ctx.confirmation.request_retry()` with error_info JSON, (3) on "retry" → return None (loop continues, re-calls LLM with same state), (4) on "cancel" → delete checkpoint, return error string, (5) on "timeout" → return error string (checkpoint NOT deleted).
- [ ] 4.3 Add `checkpoint_store` and `checkpoint_enabled` fields to `ReactContext` dataclass (optional, default None / True).
- [ ] 4.4 Modify `react_loop()` signature: add optional `initial_state: Optional[_LoopState] = None` parameter. When provided, initialize `state` from it instead of creating fresh `_LoopState`. The resumed run reuses the checkpoint's stored `trace_id`.
- [ ] 4.5 Add checkpoint deletion on successful run completion in `react_loop()`: after the loop exits with a non-error result, if `ctx.checkpoint_store` and a checkpoint exists for the current `trace_id`, delete it.
- [ ] 4.6 Write integration tests using `tests/execution_harness.py`: simulate LLM timeout → verify checkpoint written → simulate retry → verify loop resumes from saved step → verify checkpoint deleted on success. Test cancel path (checkpoint deleted). Test timeout path (checkpoint survives).
- [ ] 4.7 Write test for `checkpoint_enabled=false` branch: LLM error occurs → no checkpoint file written → inline retry still works from in-memory state → `/resume` reports "No unfinished runs to resume".

## 5. Agent Controller Wiring

- [ ] 5.1 Modify `AgentController.__init__` to accept and store a `CheckpointStore` instance.
- [ ] 5.2 Modify `AgentController.run()` to accept optional `resume_from: Optional[str] = None` parameter. When provided, load checkpoint from store, extract `user_goal` and `_LoopState`, pass `initial_state` to `react_loop()`. Use the checkpoint's stored `trace_id` for the run.
- [ ] 5.3 Wire `CheckpointStore` instance into `ReactContext.checkpoint_store` in `AgentRuntime.build_react_context()` or the controller's context assembly.
- [ ] 5.4 Write tests for `AgentController.run(resume_from=...)`: verify checkpoint loaded, `initial_state` passed to `react_loop`, trace_id reused.

## 6. Telegram Error Card and Callbacks

- [ ] 6.1 Add `__LLM_ERROR__` marker handling in `_ProgressPanel.dispatch_progress()` in `telegram_interface.py`. Parse `__LLM_ERROR__:{token}:{json}` → call `_send_llm_error_prompt()`.
- [ ] 6.2 Add `_send_llm_error_prompt(message, token, error_info)` method to `TelegramInterface`. Renders error card with: error type icon + message, model name, step/max-steps, preserved tool results count, truncated detail (200 chars). Inline keyboard: [🔄 Retry] + [❌ Cancel] when retryable, [❌ Cancel] only when non-retryable. Callback data: `llm_retry:{token}:retry` / `llm_retry:{token}:cancel`.
- [ ] 6.3 Add `llm_retry` callback handler in `telegram_callbacks.py`. Parse `llm_retry:{token}:{response}` → call `agent.confirmation.signal_retry(token, response)`. Update the error card message to show the user's choice.
- [ ] 6.4 Write tests for error card rendering: retryable card shows both buttons, non-retryable shows only Cancel, callback data format correct, `signal_retry` called on button press, typing indicator persists while error card is shown (until user responds or retry timeout expires).

## 7. /resume Command

- [ ] 7.1 Add `/resume` command handler in `telegram_commands.py`. No args → load most recent checkpoint → resume. With number arg (`/resume N`) → load Nth checkpoint. Multiple checkpoints → list with numbers, goal text, step/max-steps, error type, age. No checkpoints → "No unfinished runs to resume." Agent busy → "⚠️ Agent is currently running. Wait or /stop it first."
- [ ] 7.2 Add `/resume` to the bot command registration list and help text in `telegram_interface.py`.
- [ ] 7.3 Handle corrupted checkpoint in `/resume`: delete file, report corruption, continue listing remaining. Handle non-retryable checkpoint in `/resume`: refuse to resume, inform user, offer to delete.
- [ ] 7.4 Write tests for `/resume`: single checkpoint resumes, multiple checkpoints list, `/resume N` selects, no checkpoints message, busy agent rejection, corrupted checkpoint deletion, non-retryable refusal.

## 8. Startup Checkpoint Scan

- [ ] 8.1 Add startup scan in `main.py`: after agent initialization, call `checkpoint_store.list()`. If checkpoints found, send notification to operator: "💾 Found unfinished run: '{goal}'. Send /resume to continue." If multiple, mention the most recent and instruct to `/resume` to see all.
- [ ] 8.2 Write test for startup scan: checkpoints exist → notification sent, no checkpoints → no notification.

## 9. Fix "✅ Done" Bug

- [ ] 9.1 Modify `_classify_final_status()` in `telegram_interface.py`: add check for result string starting with "❌" → return "failed". Keep existing "[Cancelled]" → "cancelled" check. Everything else → "done".
- [ ] 9.2 Modify `_run_agent_task_locked()` in `telegram_interface.py`: use `_classify_final_status()` result to show "❌ Failed" instead of "✅ Done" when status is "failed".
- [ ] 9.3 Write tests for `_classify_final_status`: "❌ LLM error: ..." → "failed", "[Cancelled]" → "cancelled", "Here is your answer." → "done", "" → "done".

## 10. Scheduled Job Failure Classification

- [ ] 10.1 Add error classification helper in `scheduler.py`: inspect sub-agent result string for error patterns (starts with "❌" or contains known error type strings like "TimeoutException", "RateLimitError", "quota"). Return error type string for notification.
- [ ] 10.2 Modify `_run_via_spawn_agent()` and `_run_via_agent_fn()` notification paths: when failure detected, include error type in the notification message. Include next scheduled run time from `_jobs_meta[tag].get("_next_run")`.
- [ ] 10.3 Modify `execution_log.record()` call to include error type in the log entry when a failure is classified.
- [ ] 10.4 Write tests for scheduled job failure classification: timeout result → classified + notified with type + next run, success result → normal notification, error type recorded in execution log.

## 11. Configuration

- [ ] 11.1 Add `[llm_error_handling]` section to `config_schema.py`: `retry_timeout_seconds: int = 120`, `checkpoint_enabled: bool = True`. Add to `AppConfig` dataclass.
- [ ] 11.2 Wire `retry_timeout_seconds` into `ConfirmationManager.request_retry()` timeout and `checkpoint_enabled` into `ReactContext.checkpoint_enabled`.
- [ ] 11.3 Add `[llm_error_handling]` section to `config.toml.example` with documented defaults.
- [ ] 11.4 Write tests for config parsing: section present → values loaded, section absent → defaults applied.

## 12. Documentation and Whitelist

- [ ] 12.1 Update `README.md`: document error handling behavior (error types, inline retry, `/resume` command, checkpoint crash recovery, scheduled job failure notification).
- [ ] 12.2 Update `vulture_whitelist.py`: add new public symbols (`CheckpointStore`, `LLMErrorInfo`, `RETRY_PREFIX`, `request_retry`, `signal_retry`).
- [ ] 12.3 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` to verify no lint/dead-code issues.

## 13. Validation

- [ ] 13.1 Run `make check` (lint + test) and ensure all tests pass.
- [ ] 13.2 Run `openspec validate llm-timeout-hardening --type change --strict` to verify change artifacts are valid.
- [ ] 13.3 Verify `LLMCancelledError` is indeed a `RuntimeError` subclass (not `LLMError`) by checking `providers/_errors.py` — this confirms the design's factual claim about the exception hierarchy.