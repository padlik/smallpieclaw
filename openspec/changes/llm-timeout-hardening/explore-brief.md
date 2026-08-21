# Explore Brief: LLM Timeout Hardening

## Problem

LLM timeout/network errors are poorly handled:
1. **No user notification** — user sees "✅ Done" + error text in body, must guess the cause
2. **No retry option** — user must retype the entire prompt from scratch
3. **No state preservation** — all tool results and conversation context are lost on retry
4. **Scheduled jobs** — failures not clearly distinguished from successes

## Rejected Alternatives

- **In-memory checkpoints** — lost on crash/restart; user can't resume after process kill
- **Provider-level error subtypes** (new exception classes) — touches 6 provider files for marginal benefit; existing exception types are already distinguishable at react_loop level via `type(exc)` inspection
- **"Retry with different model" button** — user can `/model` then `/resume`; not worth the UI complexity
- **Periodic checkpoints (every N steps)** — crash-during-tool-execution is rare; error-only checkpoints cover the main use case
- **Loop-level auto-retry** — provider already retried 3x with backoff; one more attempt won't help
- **Scheduled job auto-retry** — next cron run is the natural retry; no user to press "Retry"
- **Checkpoint cleanup (TTL, max files, eviction)** — checkpoints are small, few, and deleted on success/cancel; stale files are harmless and manageable via `/resume` listing
- **/discard command** — stale files are harmless; `/resume` just ignores unwanted ones

## Final Approach

### Error Classification (react_loop.py only, no provider changes)

Classify by inspecting `type(exc)` and message content at the `_request_turn` level:

| Exception | Error Type | User Message | Retryable |
|---|---|---|---|
| `httpx.TimeoutException` | `timeout` | "⏱️ Request timed out after {N}s" | Yes |
| `httpx.ConnectError` | `connection` | "🔌 Connection failed" | Yes |
| `httpx.HTTPStatusError` 429 | `rate_limit` | "🚫 Rate limit reached" | Yes |
| `LLMEmptyResponseError` | `empty` | "📭 Model returned no content" | Yes |
| `LLMContextOverflowError` | `context` | "📏 Context too long" | No |
| `LLMPermanentError` (4xx) | `permanent` | "❌ {detail}" | No |
| Other `LLMError` | `unknown` | "❌ LLM error: {detail}" | Yes |

### Disk-Persisted Checkpoints (checkpoint_store.py, new module)

- **Write**: only when LLM error occurs (before retry prompt)
- **Delete**: on success or explicit cancel (one `os.unlink` each)
- **No cleanup**: no TTL, no max files, no eviction, no startup scan
- **Atomic write**: tmp file → `os.replace` (same pattern as prompt_registry)
- **Location**: `data/run_checkpoints/{trace_id}.json`
- **Contents**: `{trace_id, user_goal, messages, step, goal_idx, max_steps, json_fail_streak, model, created_at, error_info}`

### Inline Retry Prompt (confirmation.py + telegram)

- Blocks agent thread (same pattern as `__CONFIRM__` and `__EXTEND__`)
- New `__LLM_ERROR__:{token}:{json}` progress marker
- Two buttons: `[🔄 Retry]` `[❌ Cancel]`
- 120s timeout → treated as cancel for the agent thread, BUT checkpoint is NOT deleted (user can `/resume` later)
- New `ConfirmationManager.request_retry()` / `signal_retry()` methods

### /resume Command (telegram_commands.py)

- `/resume` (no args) → resumes most recent checkpoint
- `/resume N` → resumes Nth checkpoint from list
- Multiple checkpoints → lists them with numbers
- Agent busy → "Wait or /stop first"
- Corrupted checkpoint → delete + report
- No checkpoints → "No unfinished runs to resume"
- Startup scan → notification if checkpoints found: "💾 Found unfinished run: '{goal}'. Send /resume to continue."

### Fix "✅ Done" Bug (telegram_interface.py)

- `_classify_final_status()` checks result string for error indicators (starts with "❌")
- Shows "❌ Failed" instead of "✅ Done" for error results

### Scheduled Job Notification (scheduler.py)

- Classify error in sub-agent result string
- Notify with error type + next scheduled run time
- No auto-retry — next cron = natural retry

## Cross-Module Data Flows

### Error → Checkpoint → Retry flow

```
react_loop._request_turn()
  → LLM call fails (after provider retries exhausted)
  → _classify_llm_error(exc) → LLMErrorInfo(type, message, retryable, detail)
  → checkpoint_store.save(trace_id, state, error_info)
  → progress_cb("__LLM_ERROR__:{token}:{json}")
  → confirmation.request_retry(token, progress_cb) — blocks agent thread
  → User response:
     "retry" → load checkpoint, continue loop from saved step
     "cancel" → checkpoint_store.delete(trace_id), return error string
     timeout(120s) → return error string (checkpoint stays on disk)
```

### /resume flow

```
telegram_commands.handle_resume()
  → checkpoint_store.list() → list of checkpoints
  → if 0: "No unfinished runs"
  → if 1: load and resume
  → if >1: list with numbers, wait for /resume N
  → agent_controller.run(goal, resume_from=checkpoint)
  → react_loop(ctx, goal, initial_state=checkpoint.state)
  → on success: checkpoint_store.delete(trace_id)
```

### Telegram callback flow

```
telegram_callbacks.handle_llm_retry(callback_data)
  → parse: llm_retry:{token}:{response}
  → confirmation.signal_retry(token, response)
  → unblocks agent thread in react_loop
```

## Open Questions

1. Should the 120s retry-prompt timeout be configurable? → Yes, `retry_timeout_seconds` in config (default 120)
2. Should checkpoint_enabled be configurable? → Yes, boolean in config (default true)
3. What happens if user sends a new message while retry prompt is showing? → Same as current deferred-message pattern: message is queued, shown after current run ends
4. Should /resume work for sub-agents? → No, only main agent. Sub-agents are ephemeral.
5. Should the error card show how many tool results are preserved? → Yes, count messages with tool results in checkpoint