## Why

LLM timeout and network errors are silently swallowed: the user sees "✅ Done" with an error string buried in the message body, has no way to retry without retyping the entire prompt, and loses all tool results and conversation context accumulated during the run. Scheduled jobs that fail due to LLM errors are reported as normal results with no error classification. This makes the agent unreliable for long-running tasks where transient LLM failures are common.

## What Changes

- **Error classification**: LLM errors in `react_loop._request_turn()` are classified into seven types — `timeout`, `connection`, `rate_limit`, `empty`, `context`, `permanent`, and `unknown` (for other `LLMError` subtypes) — by inspecting `type(exc)` and message content. Each type surfaces a tailored user message instead of a generic "❌ LLM error: {type}: {exc}" string. Non-retryable types (`context`, `permanent`) suppress the Retry button and show only `[❌ Cancel]`.
- **Inline retry prompt**: When a retryable LLM error occurs, the agent thread blocks and the Telegram UI shows an error card with `[🔄 Retry]` and `[❌ Cancel]` inline buttons (same thread-blocking pattern as existing tool confirmations and step-extension prompts). The error card displays the error type, model name, current step/max-steps, and a count of preserved tool results in the checkpoint. Non-retryable errors show only `[❌ Cancel]`.
- **Disk-persisted checkpoints**: On LLM error, the full `_LoopState` is written to `data/run_checkpoints/{trace_id}.json` using atomic write (tmp → `os.replace`). The checkpoint contains: `trace_id`, `user_goal`, `messages` (full conversation history with tool results), `step`, `goal_idx`, `max_steps`, `json_fail_streak`, `model`, `created_at`, and `error_info` (type, message, retryable, detail). On retry, the loop resumes from the saved state — all prior tool results are preserved. The checkpoint is deleted on successful run completion or when the user explicitly presses Cancel. A 120s retry-prompt timeout unblocks the agent thread (returning an error string) but does **not** delete the checkpoint — the user can `/resume` later.
- **`/resume` command**: New Telegram command that loads the most recent checkpoint (or a specific one by number when multiple exist) and resumes the run from the saved state. Enables crash recovery: if the process is killed during a retry prompt, the checkpoint survives on disk and can be resumed after restart. Edge cases: if the agent is currently busy, shows "Wait or /stop first"; if a checkpoint is corrupted, deletes it and reports the corruption; if no checkpoints exist, shows "No unfinished runs to resume"; if a new message arrives while the retry prompt is showing, it is deferred (same as the existing deferred-message pattern). `/resume` applies to the main agent only, not sub-agents.
- **Startup checkpoint scan**: On process startup, `main.py` scans `data/run_checkpoints/` for existing checkpoint files. If found, sends a notification: "💾 Found unfinished run: '{goal}'. Send /resume to continue."
- **Fix "✅ Done" bug**: `_classify_final_status()` in `telegram_interface.py` now detects error result strings (starting with "❌") and shows "❌ Failed" instead of "✅ Done".
- **Scheduled job failure notification**: Sub-agent results from scheduled jobs are inspected for error patterns; failures are notified with error type and next scheduled run time. No auto-retry — the next cron run is the natural retry.
- **New config section** `[llm_error_handling]` with two settings: `retry_timeout_seconds` (default 120) and `checkpoint_enabled` (default true).

## Capabilities

### New Capabilities
- `llm-error-recovery`: Error classification, inline retry prompts, disk-persisted checkpoints, `/resume` command, and scheduled-job failure notification for recovering from LLM timeout/network errors during both interactive runs and scheduled jobs.

### Modified Capabilities
- `telegram-command-surface`: New `/resume` command for resuming interrupted runs from disk checkpoints.
- `telegram-progress-panel`: New `__LLM_ERROR__` progress marker handling and error card rendering with inline retry/cancel buttons.
- `prompt-tracking`: `_classify_final_status()` now distinguishes failed runs from successful ones, so the prompt registry records accurate terminal status.

## Impact

- **New module**: `checkpoint_store.py` — `CheckpointStore` class with `save()`, `load()`, `delete()`, `list()` methods and atomic writes.
- **Modified**: `react_loop.py` — `_request_turn()` error handling, new `_classify_llm_error()` and `_handle_llm_error()` functions, `react_loop()` accepts optional checkpoint for resume.
- **Modified**: `confirmation.py` — new `request_retry()` / `signal_retry()` methods and `RETRY_PREFIX` constant.
- **Modified**: `agent_controller.py` — wires `CheckpointStore`, `run()` accepts `resume_from` parameter.
- **Modified**: `main.py` — startup scan of `data/run_checkpoints/` with notification for recoverable runs.
- **Modified**: `telegram_interface.py` — `__LLM_ERROR__` marker handling, `_send_llm_error_prompt()`, fix `_classify_final_status()`.
- **Modified**: `telegram_callbacks.py` — new `llm_retry` callback handler.
- **Modified**: `telegram_commands.py` — new `/resume` command handler.
- **Modified**: `scheduler.py` — error classification in sub-agent result notification.
- **Modified**: `config_schema.py` — new `[llm_error_handling]` section.
- **Modified**: `config.example.toml` — document new config section.
- **Modified**: `README.md` — document error handling, retry, and resume behavior.
- **Modified**: `vulture_whitelist.py` — add new public symbols.
- **No provider changes**: Error classification is done at the `react_loop` level by inspecting existing exception types. No new exception classes, no changes to `providers/` modules.