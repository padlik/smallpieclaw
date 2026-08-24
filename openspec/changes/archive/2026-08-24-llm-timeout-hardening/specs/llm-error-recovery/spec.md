## ADDED Requirements

### Requirement: LLM errors are classified into typed categories

The system SHALL classify LLM errors caught in `react_loop._request_turn()` into seven typed categories by inspecting `type(exc)` and message content. Each category SHALL produce a tailored user-facing message instead of a generic error string. The seven categories are: `timeout` (httpx.TimeoutException), `connection` (httpx.ConnectError), `rate_limit` (httpx.HTTPStatusError with 429 status), `empty` (LLMEmptyResponseError), `context` (LLMContextOverflowError), `permanent` (LLMPermanentError), and `unknown` (any otherwise-unclassified exception, including non-429 HTTPStatusError, generic LLMError, or other exceptions). `LLMCancelledError` SHALL NOT be classified — it propagates immediately without checkpoint, error card, or retry prompt. No new exception classes SHALL be created in provider modules.

Feature: LLM error recovery
Rule: The user sees a meaningful error message, not a raw exception dump.

#### Scenario: Timeout error is classified and shown to user
- **GIVEN** the LLM call raises `httpx.TimeoutException` after all provider retries are exhausted
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `timeout`
- **AND** the user-facing message starts with "⏱️ Request timed out"
- **AND** the error detail is logged to the structured log

#### Scenario: Connection error is classified and shown to user
- **GIVEN** the LLM call raises `httpx.ConnectError`
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `connection`
- **AND** the user-facing message starts with "🔌 Connection failed"

#### Scenario: Rate limit error is classified and shown to user
- **GIVEN** the LLM call raises `httpx.HTTPStatusError` with status code 429
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `rate_limit`
- **AND** the user-facing message starts with "🚫 Rate limit reached"

#### Scenario: Empty response error is classified and shown to user
- **GIVEN** the LLM call raises `LLMEmptyResponseError`
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `empty`
- **AND** the user-facing message starts with "📭 Model returned no content"

#### Scenario: Context overflow error is classified as non-retryable
- **GIVEN** the LLM call raises `LLMContextOverflowError`
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `context`
- **AND** the user-facing message starts with "📏 Context too long"
- **AND** the error is marked as non-retryable

#### Scenario: Permanent error is classified as non-retryable
- **GIVEN** the LLM call raises `LLMPermanentError`
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `permanent`
- **AND** the user-facing message starts with "❌"
- **AND** the error is marked as non-retryable

#### Scenario: Unknown error is classified as retryable
- **GIVEN** the LLM call raises an exception not matching any specific category (e.g., non-429 HTTPStatusError, generic LLMError, or other exception)
- **WHEN** `_request_turn()` catches the exception
- **THEN** the error is classified as type `unknown`
- **AND** the user-facing message starts with "❌ LLM error:"
- **AND** the error is marked as retryable

#### Scenario: User cancellation is not classified or checkpointed
- **GIVEN** the user runs `/stop` during an LLM call, raising `LLMCancelledError`
- **WHEN** `_request_turn()` receives the exception
- **THEN** `LLMCancelledError` propagates immediately without classification
- **AND** no checkpoint file is written
- **AND** no error card or retry prompt is shown
- **AND** the run terminates as cancelled

### Requirement: Disk-persisted checkpoint on LLM error

The system SHALL write a checkpoint file to `data/run_checkpoints/{trace_id}.json` when an LLM error occurs in `react_loop._request_turn()`. The checkpoint SHALL contain the full `_LoopState` (messages, step, goal_idx, max_steps, json_fail_streak) plus metadata (trace_id, user_goal, model, created_at, error_info with type, message, retryable, detail). The write SHALL be atomic (write to `.tmp` file, then `os.replace`). Checkpoint write failure (OSError) SHALL be non-fatal — the retry prompt proceeds without a checkpoint, and the user is still offered inline retry but cannot `/resume` after a crash.

Feature: LLM error recovery
Rule: The checkpoint is the recovery unit — it captures everything needed to resume the run without re-executing prior tools.

#### Scenario: Checkpoint is written on LLM error
- **GIVEN** the agent has completed 3 tool calls (step 3) and the LLM call fails with a timeout
- **WHEN** `_request_turn()` catches the exception
- **THEN** a checkpoint file is written to `data/run_checkpoints/{trace_id}.json`
- **AND** the file contains all 3 tool results in the messages list
- **AND** the file contains step=3, goal_idx, max_steps, json_fail_streak, model, created_at
- **AND** the file contains error_info with type="timeout", retryable=true

#### Scenario: Checkpoint write is atomic
- **GIVEN** an LLM error occurs and the checkpoint is being written
- **WHEN** the write is in progress
- **THEN** the data is first written to `{trace_id}.json.tmp`
- **AND** then `os.replace` atomically renames it to `{trace_id}.json`
- **AND** a crash during the write leaves no partial `.json` file

#### Scenario: Checkpoint write failure is non-fatal
- **GIVEN** the disk is full when the checkpoint is being written
- **WHEN** `CheckpointStore.save()` catches `OSError`
- **THEN** a warning is logged
- **AND** the retry prompt still proceeds (inline retry works)
- **AND** the user is not blocked by the write failure

#### Scenario: Checkpoint is deleted on successful run completion
- **GIVEN** a checkpoint exists from a prior LLM error and the user pressed Retry
- **WHEN** the resumed run completes successfully
- **THEN** the checkpoint file is deleted from disk

#### Scenario: Checkpoint is deleted on explicit cancel
- **GIVEN** a checkpoint exists and the user presses the Cancel button on the error card
- **WHEN** the cancel signal is received
- **THEN** the checkpoint file is deleted from disk

#### Scenario: Checkpoint survives retry-prompt timeout
- **GIVEN** a checkpoint exists and the retry prompt is shown to the user
- **WHEN** the 120s retry-prompt timeout expires without user action
- **THEN** the agent thread unblocks and returns an error string
- **AND** the checkpoint file is NOT deleted
- **AND** the user can `/resume` the run later

### Requirement: Inline retry prompt with error card

The system SHALL block the agent thread and present an inline error card with retry/cancel buttons when a retryable LLM error occurs. The error card SHALL display: error type icon and classified message, model name, current step/max-steps, count of preserved tool results in the checkpoint, and truncated error detail (first 200 chars). For non-retryable errors (context, permanent), the card SHALL show only a Cancel button — no Retry button. The agent thread blocks on a threading.Event with a configurable timeout (default 120s).

Feature: LLM error recovery
Rule: The user gets a clear error card with a choice — retry from where it left off, or cancel.

#### Scenario: Retryable error shows Retry and Cancel buttons
- **GIVEN** a timeout error occurs and the checkpoint is written
- **WHEN** the error card is rendered in Telegram
- **THEN** the card shows the timeout icon and message
- **AND** the card shows the model name and step/max-steps
- **AND** the card shows the count of preserved tool results
- **AND** the card shows a [🔄 Retry] button and a [❌ Cancel] button

#### Scenario: Non-retryable error shows only Cancel button
- **GIVEN** a context overflow error occurs (non-retryable)
- **WHEN** the error card is rendered in Telegram
- **THEN** the card shows the context overflow message
- **AND** the card shows only a [❌ Cancel] button
- **AND** no [🔄 Retry] button is shown

#### Scenario: User presses Retry and run resumes from checkpoint
- **GIVEN** a retryable error occurred and the error card is showing
- **WHEN** the user presses [🔄 Retry]
- **THEN** the agent thread unblocks
- **AND** the loop resumes from the saved step with the saved messages
- **AND** prior tool results are not re-executed
- **AND** a new LLM call is made with the full conversation context

#### Scenario: User presses Cancel and checkpoint is deleted
- **GIVEN** an error occurred and the error card is showing
- **WHEN** the user presses [❌ Cancel]
- **THEN** the agent thread unblocks
- **AND** the checkpoint file is deleted from disk
- **AND** the run returns an error string to the Telegram layer

#### Scenario: Retry prompt times out without user action
- **GIVEN** an error occurred and the error card is showing
- **WHEN** 120 seconds pass without user action
- **THEN** the agent thread unblocks
- **AND** the run returns an error string
- **AND** the checkpoint file remains on disk for `/resume`

#### Scenario: New message during retry prompt is deferred
- **GIVEN** an error occurred and the retry prompt is showing
- **WHEN** the user sends a new message while the prompt is active
- **THEN** the new message is deferred (same as existing deferred-message pattern)
- **AND** the deferred message is presented after the current run ends

### Requirement: /resume command for checkpoint recovery

The system SHALL provide a `/resume` Telegram command that loads a checkpoint from disk and resumes the run from the saved state. `/resume` with no arguments resumes the most recent checkpoint. When multiple checkpoints exist, `/resume` lists them with numbers and `/resume N` selects the Nth checkpoint. The resumed run reuses the checkpoint's stored trace_id for log correlation and correct checkpoint deletion on success.

Feature: LLM error recovery
Rule: The user can recover an interrupted run even after the process crashed and restarted.

#### Scenario: /resume with single checkpoint resumes immediately
- **GIVEN** one checkpoint exists on disk from a prior timeout
- **WHEN** the user runs `/resume`
- **THEN** the checkpoint is loaded
- **AND** the run resumes from the saved step with the saved messages
- **AND** the user sees "💾 Resuming: {goal} (step {N}/{max})"

#### Scenario: /resume with multiple checkpoints lists them
- **GIVEN** two checkpoints exist on disk from different failed runs
- **WHEN** the user runs `/resume`
- **THEN** the response lists both checkpoints with numbers
- **AND** each entry shows the goal text, step/max-steps, error type, and age
- **AND** the user is prompted to run `/resume N` to select one

#### Scenario: /resume N selects a specific checkpoint
- **GIVEN** two checkpoints exist and the user ran `/resume` to see the list
- **WHEN** the user runs `/resume 2`
- **THEN** the second checkpoint is loaded and the run resumes

#### Scenario: /resume with no checkpoints informs the user
- **GIVEN** no checkpoint files exist in `data/run_checkpoints/`
- **WHEN** the user runs `/resume`
- **THEN** the response says "No unfinished runs to resume."

#### Scenario: /resume while agent is busy is rejected
- **GIVEN** the agent is currently running a task
- **WHEN** the user runs `/resume`
- **THEN** the response says "⚠️ Agent is currently running. Wait for it to finish or /stop it first."

#### Scenario: /resume on corrupted checkpoint deletes it and reports
- **GIVEN** a checkpoint file exists but is not valid JSON or is missing required fields
- **WHEN** the user runs `/resume`
- **THEN** the corrupted file is deleted
- **AND** the response reports the corruption
- **AND** remaining valid checkpoints are still listed

#### Scenario: /resume refuses non-retryable checkpoint
- **GIVEN** a checkpoint exists with error_info.retryable = false (e.g., context overflow)
- **WHEN** the user runs `/resume`
- **THEN** the response refuses to resume the non-retryable checkpoint
- **AND** the response informs the user that the error is non-retryable
- **AND** the response offers to delete the checkpoint

#### Scenario: /resume is main-agent only
- **GIVEN** a sub-agent has a checkpoint (hypothetically)
- **WHEN** the user runs `/resume`
- **THEN** sub-agent checkpoints are not listed
- **AND** only main-agent checkpoints are available for resume

#### Scenario: Resumed run reuses the checkpoint's trace_id
- **GIVEN** a checkpoint exists with trace_id "r-a1b2c3d4"
- **WHEN** the user runs `/resume` and the run starts
- **THEN** the resumed run uses trace_id "r-a1b2c3d4" for log correlation
- **AND** on successful completion, the checkpoint file "r-a1b2c3d4.json" is deleted

### Requirement: Startup checkpoint scan notification

The system SHALL scan `data/run_checkpoints/` on process startup. If checkpoint files are found, the system SHALL send a notification to the operator: "💾 Found unfinished run: '{goal}'. Send /resume to continue." If no checkpoints are found, no notification is sent.

Feature: LLM error recovery
Rule: The user is proactively notified about recoverable runs after a crash/restart.

#### Scenario: Startup with existing checkpoints sends notification
- **GIVEN** the process is starting up and `data/run_checkpoints/` contains one checkpoint file
- **WHEN** the startup scan runs
- **THEN** a notification is sent to the operator
- **AND** the notification includes the goal text from the checkpoint
- **AND** the notification instructs the user to send `/resume`

#### Scenario: Startup with no checkpoints sends no notification
- **GIVEN** the process is starting up and `data/run_checkpoints/` is empty or does not exist
- **WHEN** the startup scan runs
- **THEN** no notification is sent

#### Scenario: Startup with multiple checkpoints notifies about the most recent
- **GIVEN** the process is starting up and `data/run_checkpoints/` contains three checkpoint files
- **WHEN** the startup scan runs
- **THEN** a notification is sent mentioning the most recent checkpoint
- **AND** the notification instructs the user to send `/resume` to see all available checkpoints

### Requirement: Scheduled job failure classification and notification

The system SHALL inspect scheduled job sub-agent result strings for error patterns. When a failure is detected (result starts with "❌" or matches known error type strings), the system SHALL classify the error type and notify the operator with the error type and the next scheduled run time. No auto-retry SHALL be performed — the next cron run is the natural retry.

Feature: LLM error recovery
Rule: Scheduled job failures are classified and reported, not silently passed as normal results.

#### Scenario: Scheduled job timeout failure is classified and notified
- **GIVEN** a scheduled job's sub-agent returns a result string starting with "❌ LLM error: TimeoutException"
- **WHEN** the scheduler processes the result
- **THEN** the failure is classified as a timeout error
- **AND** the notification includes "⚠️ Scheduled job failed: {tag}" with the error type
- **AND** the notification includes the next scheduled run time

#### Scenario: Scheduled job success is notified normally
- **GIVEN** a scheduled job's sub-agent returns a successful result string
- **WHEN** the scheduler processes the result
- **THEN** the notification is sent as a normal scheduled result
- **AND** no error classification is applied

#### Scenario: Scheduled job failure is recorded in execution log
- **GIVEN** a scheduled job fails with an LLM error
- **WHEN** the failure is recorded in the execution log
- **THEN** the log entry includes the error type classification
- **AND** the log entry includes the failure timestamp

### Requirement: LLM error handling configuration

The system SHALL support a `[llm_error_handling]` configuration section with two settings: `retry_timeout_seconds` (integer, default 120) controlling how long the retry prompt waits for user input, and `checkpoint_enabled` (boolean, default true) controlling whether disk checkpoints are written. When `checkpoint_enabled` is false, inline retry still works (the agent thread holds `_LoopState` in memory), but `/resume` and crash recovery are not available.

Feature: LLM error recovery
Rule: Minimal config — two settings with sensible defaults.

#### Scenario: Default config applies when section is absent
- **GIVEN** the configuration file does not contain a `[llm_error_handling]` section
- **WHEN** the agent starts
- **THEN** `retry_timeout_seconds` defaults to 120
- **AND** `checkpoint_enabled` defaults to true

#### Scenario: Checkpoint disabled still allows inline retry
- **GIVEN** `checkpoint_enabled` is set to false in config
- **WHEN** an LLM error occurs
- **THEN** no checkpoint file is written to disk
- **AND** the inline retry prompt still appears with Retry and Cancel buttons
- **AND** pressing Retry resumes the run from in-memory state
- **AND** `/resume` reports "No unfinished runs to resume" (no checkpoints on disk)