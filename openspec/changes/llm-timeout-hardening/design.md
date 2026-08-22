## Context

The agent's ReAct loop (`react_loop.py`) makes LLM calls via `_request_turn()`. When a call fails — timeout, connection error, rate limit, empty response — the provider-level retry layer (`_with_retry` in `providers/_utils.py`) retries up to `max_retries` times with exponential backoff. If all retries are exhausted, the exception propagates to `_request_turn()`, which catches it with a broad `except Exception` and returns an error string via `_Turn.early_return`. The loop immediately exits, discarding all state (`_LoopState` is local to `react_loop()`).

The Telegram layer (`telegram_interface.py`) receives the error string as the "result" and displays "✅ Done" unconditionally, followed by the error text in the message body. The user has no retry option, no error classification, and no way to preserve the tool results accumulated during the run. Scheduled jobs (`scheduler.py`) report LLM failures as normal result strings with no error classification.

Key existing patterns this design builds on:
- **ConfirmationManager** (`confirmation.py`): thread-blocking pattern with `__CONFIRM__` / `__EXTEND__` progress markers → Telegram inline buttons. The retry prompt follows this exact pattern.
- **Prompt registry** (`prompt_registry.py`): atomic write (tmp → `os.replace`) for crash-safe persistence. Checkpoints use the same technique.
- **`_LoopState`** (`react_loop.py`): dataclass holding `messages`, `step`, `goal_idx`, `max_steps`, `json_fail_streak`. This is the state to checkpoint.
- **Exception hierarchy** (`providers/_errors.py`): `LLMError` → `LLMPermanentError`, `LLMEmptyResponseError`, `LLMContextOverflowError`. (`LLMCancelledError` is a separate `RuntimeError` subclass, not an `LLMError` — it propagates immediately and is never classified.) Plus `httpx.TimeoutException`, `httpx.ConnectError`, `httpx.HTTPStatusError`. All already distinguishable via `type(exc)`.

## Goals / Non-Goals

**Goals:**
- Classify LLM errors into seven types and surface tailored user messages
- Show inline retry/cancel buttons on retryable errors (thread-blocking, same as confirmations)
- Persist full `_LoopState` to disk on LLM error for crash recovery and state preservation
- Resume runs from checkpoints via inline retry or `/resume` command
- Fix the "✅ Done" bug so failed runs show "❌ Failed"
- Classify and notify scheduled job failures with error type and next run time
- Minimal config: two settings (`retry_timeout_seconds`, `checkpoint_enabled`)

**Non-Goals:**
- No new exception classes in providers — classification is done at `react_loop` level by inspecting `type(exc)`
- No "retry with different model" button — user can `/model` then `/resume`
- No periodic checkpoints — checkpoints are written only on LLM error
- No TTL-based cleanup — checkpoints are deleted on success/cancel; a max_checkpoints retention cap (default 20) prunes the oldest on each save() to prevent unbounded growth; corrupted files are removed during pruning
- No loop-level auto-retry — provider already retried 3x; go straight to user prompt
- No scheduled job auto-retry — next cron run is the natural retry
- No streaming response caching — the agent doesn't stream; `_LoopState.messages` is the preserved state

## Decisions

### D1: Error classification at react_loop level, not provider level

**Decision:** Classify errors in `_request_turn()` by inspecting `type(exc)` and message content. No changes to `providers/` modules.

**Rationale:** The exception types are already distinct at the `_request_turn` catch point: `httpx.TimeoutException`, `httpx.ConnectError`, `httpx.HTTPStatusError` (with `.response.status_code`), `LLMEmptyResponseError`, `LLMContextOverflowError`, `LLMPermanentError`, generic `LLMError`. Creating new provider-level subtypes would touch 6 files for marginal benefit — the classification function is ~30 lines and lives in one place.

**Full classification mapping:**

| Exception | Error Type | User Message | Retryable |
|---|---|---|---|
| `httpx.TimeoutException` | `timeout` | "⏱️ Request timed out after {N}s" | Yes |
| `httpx.ConnectError` | `connection` | "🔌 Connection failed" | Yes |
| `httpx.HTTPStatusError` (429) | `rate_limit` | "🚫 Rate limit reached" | Yes |
| `LLMEmptyResponseError` | `empty` | "📭 Model returned no content" | Yes |
| `LLMContextOverflowError` | `context` | "📏 Context too long" | No |
| `LLMPermanentError` (4xx) | `permanent` | "❌ {detail}" | No |
| Other `LLMError` | `unknown` | "❌ LLM error: {detail}" | Yes |

**Alternatives considered:**
- New exception classes in `providers/_errors.py` + raising them in each provider — rejected: 6 file changes, no behavioral difference, more surface area for bugs.

### D2: Disk-persisted checkpoints with atomic writes

**Decision:** New `checkpoint_store.py` module with `CheckpointStore` class. Checkpoints written to `data/run_checkpoints/{trace_id}.json` using atomic write (write to `.tmp` → `os.replace`). Written only on LLM error. Deleted on success or explicit cancel.

**Rationale:** Disk persistence survives process crashes/restarts — the user can `/resume` even after the agent was killed. Atomic writes prevent corruption from partial writes during crashes (same pattern as `prompt_registry.py` ADR-0014). The checkpoint contains the full `_LoopState` plus metadata (`user_goal`, `model`, `created_at`, `error_info`) needed for `/resume` listing and error card rendering.

**Checkpoint JSON structure:**
```json
{
  "trace_id": "r-a1b2c3d4",
  "user_goal": "Analyze the logs and summarize errors",
  "messages": [
    {"role": "user", "content": "Analyze the logs..."},
    {"role": "assistant", "content": "I'll use shell..."},
    {"role": "user", "content": "tool result: ls -la..."},
    ...
  ],
  "step": 5,
  "goal_idx": 0,
  "max_steps": 8,
  "json_fail_streak": 0,
  "model": "gpt-4o",
  "created_at": "2026-08-21T14:30:00Z",
  "error_info": {
    "type": "timeout",
    "message": "⏱️ Request timed out after 30s",
    "retryable": true,
    "detail": "httpx.TimeoutException: ..."
  }
}
```

**Alternatives considered:**
- In-memory checkpoints in `AgentController` — rejected: lost on crash/restart, can't resume after process kill.
- Periodic checkpoints every N steps — rejected: adds I/O overhead and complexity for a rare edge case (crash during tool execution without LLM error).
- No retention cap — rejected: unbounded disk growth from stale checkpoints in long-running deployments. A max_checkpoints cap (default 20) with pruning on save() is a simple, low-overhead solution.

### D3: Thread-blocking retry prompt (ConfirmationManager pattern)

**Decision:** New `ConfirmationManager.request_retry()` / `signal_retry()` methods. The agent thread blocks on a `threading.Event` (120s timeout). The Telegram layer renders an error card with inline buttons via a new `__LLM_ERROR__:{token}:{json}` progress marker. The error card displays: error type icon + classified message, model name, current step/max-steps, count of preserved tool results in the checkpoint, and truncated error detail (first 200 chars).

**Rationale:** This is the exact same pattern as `request_confirmation()` (tool approval) and `request_extension()` (step extension). The agent thread blocks, the progress callback sends a marker, Telegram renders inline buttons, the user's button press calls `signal_retry()` which sets the event and unblocks the thread. No new architectural pattern — just a new instance of an existing one.

**Retry prompt lifecycle:**
```
LLM error → checkpoint written → __LLM_ERROR__ marker sent
  → agent thread blocks on event (120s)
  → User presses [🔄 Retry] → signal_retry(token, "retry") → event.set()
    → agent loads checkpoint, continues loop from saved step
  → User presses [❌ Cancel] → signal_retry(token, "cancel") → event.set()
    → checkpoint deleted, return error string
  → 120s timeout → event not set, agent unblocks
    → return error string (checkpoint NOT deleted — user can /resume)
```

**Non-retryable errors** (`context`, `permanent`): checkpoint is still written (for `/resume` listing), but the error card shows only `[❌ Cancel]` — no Retry button. If the user lets it time out, `/resume` will refuse to resume a non-retryable checkpoint and inform the user.

**Alternatives considered:**
- Post-completion prompt (non-blocking) — rejected: state already lost by the time the run ends; can't resume.
- Auto-retry at loop level — rejected: provider already retried 3x with backoff; one more attempt won't help.

### D4: /resume command — most recent checkpoint, no args

**Decision:** `/resume` (no args) loads the most recent checkpoint and resumes. If multiple checkpoints exist, lists them with numbers and `/resume N` selects one. Startup scan in `main.py` notifies if checkpoints are found.

**Rationale:** Simplest UX for the common case (one interrupted run). Multiple-checkpoint listing handles concurrent failures. Startup notification handles crash recovery proactively.

**Edge cases:**
- Agent busy → "⚠️ Agent is currently running. Wait for it to finish or /stop it first."
- Corrupted checkpoint → delete file, report corruption, continue listing remaining
- No checkpoints → "No unfinished runs to resume."
- New message while retry prompt showing → deferred (existing deferred-message pattern)
- Sub-agents → excluded; `/resume` is main-agent only
- Non-retryable checkpoint → `/resume` refuses, informs user, offers to delete

**Alternatives considered:**
- `/discard` command — rejected: stale files are harmless; `/resume` listing is sufficient.
- Fancy listing UI with per-checkpoint inline buttons — rejected: numbered list + `/resume N` is simpler and sufficient.

### D5: react_loop() accepts optional checkpoint for resume

**Decision:** `react_loop()` gains an optional `initial_state` parameter (type `Optional[_LoopState]`). When provided, the loop initializes `state` from the checkpoint instead of creating a fresh `_LoopState`. `AgentController.run()` gains a `resume_from` parameter that loads the checkpoint and passes it through. The resumed run reuses the checkpoint's stored `trace_id` — this ensures log correlation across the original and resumed run, and ensures the checkpoint file is deleted by the correct `trace_id` on successful completion.

**Rationale:** Minimal change to the loop signature. The `_LoopState` dataclass is already the unit of mutable state — just externalize its creation. The loop logic itself doesn't change; only the initialization point.

**Alternatives considered:**
- Store `_LoopState` in `ReactContext` — rejected: `ReactContext` is construction-time config, not mutable run state. Mixing them muddies the boundary.
- Separate `resume_loop()` function — rejected: duplicates the loop body for no benefit.

### D6: Scheduled job failure classification in scheduler

**Decision:** In `scheduler.py`, sub-agent result strings are inspected for error patterns (prefix "❌" or known error type strings). Failures are classified and notified with error type + next scheduled run time. No auto-retry.

**Rationale:** Scheduled jobs are unattended — there's no user to press "Retry". The next cron run is the natural retry. Classification helps the user understand whether to wait (transient) or take action (quota exhausted). The execution_log already records results; adding error type is a minor extension.

**Alternatives considered:**
- Auto-retry transient failures 2x with backoff — rejected: provider already retried 3x; adding more retries wastes quota on potentially doomed attempts.

### D7: Fix _classify_final_status() for error detection

**Decision:** `_classify_final_status()` in `telegram_interface.py` checks if the result string starts with "❌" and returns `"failed"` instead of `"done"`. The status message shows "❌ Failed" instead of "✅ Done".

**Rationale:** The current code only checks for `"[Cancelled]"`. Error strings from `_request_turn` start with "❌ LLM error:" — a simple prefix check covers all error cases without needing structured return types.

## Risks / Trade-offs

- **[Checkpoint file leaks]** → Checkpoints from timed-out retry prompts (120s) that the user never resolves will accumulate. Mitigation: a `max_checkpoints` retention cap (default 20) prunes the oldest checkpoints on each `save()` call, preventing unbounded growth. Corrupted checkpoint files are also removed during pruning. `/resume` listing makes remaining checkpoints visible.
- **[Thread blocking during retry prompt]** → The agent thread is held for up to 120s waiting for user input. Mitigation: same pattern as existing confirmations (300s timeout) and step extensions (120s timeout). The thread is idle, not consuming resources.
- **[Checkpoint write failure]** → Disk full or permissions error during checkpoint write. Mitigation: `CheckpointStore.save()` catches `OSError`, logs warning, and the retry prompt proceeds without a checkpoint (user can still retry inline, but can't `/resume` after crash). The error is non-fatal.
- **[Non-retryable checkpoint confusion]** → User might `/resume` a non-retryable error (context overflow) and hit the same error again. Mitigation: `/resume` checks `error_info.retryable` and refuses non-retryable checkpoints, informing the user.
- **[Message format compatibility on resume]** → Resumed messages may contain native tool-calling format (`tool_calls`, `tool_call_id`) from a previous model that the current model doesn't support. Mitigation: `_linearize_native_turns()` already handles this — it's called in `_request_turn()` before the LLM call, converting native turns to plain text for the json_mode fallback path.
- **[Concurrent checkpoint access]** → Two runs with the same trace_id writing to the same file. Mitigation: trace IDs are unique per run (`new_trace_id()` generates `r-<8 hex>`). No collision possible.

## Migration Plan

1. **No data migration needed** — `data/run_checkpoints/` is a new directory created on first use.
2. **No config migration needed** — new `[llm_error_handling]` section has defaults; absent config = defaults apply.
3. **Backward compatible** — existing runs without errors are unaffected. The new error handling only activates when `_request_turn()` catches an exception.
4. **Rollback** — delete `data/run_checkpoints/`, remove `[llm_error_handling]` from config. The code paths are additive; removing the config section reverts to default behavior (which is the new behavior with `retry_timeout_seconds=120` and `checkpoint_enabled=true`). To fully revert, remove the new code — but no data corruption or migration artifacts are left behind.

## Open Questions

- Should the error card show the full error detail or just the classified message? → Design decision: show classified message + truncated detail (first 200 chars). Full detail goes to logs.
- Should `/resume` work while a retry prompt is actively showing? → No — the retry prompt is already offering resume via the inline button. `/resume` is for after the prompt has timed out or the process has restarted.
- No in-force ADRs need supersession for this change. ADR-0014 (dual-write archive) is referenced as a pattern for atomic writes but not modified. The `AgentController.run()` signature change (adding `resume_from` parameter) is an extension of `agent_controller.py` as listed in the frozen proposal's Impact — no ADR-0007 (`agent_runtime.py`) modification is involved.