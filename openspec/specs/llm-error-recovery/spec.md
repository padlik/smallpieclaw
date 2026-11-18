# LLM Error Recovery Specification

## Purpose

Define how the system classifies LLM errors into typed categories, persists disk checkpoints for crash recovery, presents inline retry/cancel error cards in Telegram, provides `/resume` for recovering interrupted runs after a process restart, classifies scheduled job failures, and configures error handling behavior.

## Requirements
### Requirement: LLM errors are classified into typed categories

The system SHALL retain the seven existing categories: `timeout`, `connection`, `rate_limit`, `empty`, `context`, `permanent`, and `unknown`. Provider retry exhaustion is evaluated against the agent-owned request budget. Cancellation SHALL still propagate without classification or checkpoint.

#### Scenario: Timeout error is classified and shown to user
- **GIVEN** the logical request exhausts agent-level attempts on `httpx.TimeoutException`
- **WHEN** `_request_turn()` catches it
- **THEN** it is classified `timeout` with message starting `⏱️ Request timed out`
- **AND** its detail is structured-logged

#### Scenario: Connection error is classified and shown to user
- **GIVEN** the LLM call raises `httpx.ConnectError`
- **WHEN** `_request_turn()` catches it
- **THEN** it is classified `connection` with message starting `🔌 Connection failed`

#### Scenario: Rate limit error is classified and shown to user
- **GIVEN** the LLM call raises HTTP 429
- **WHEN** `_request_turn()` catches it
- **THEN** it is classified `rate_limit` with message starting `🚫 Rate limit reached`

#### Scenario: Empty response error is classified and shown to user
- **GIVEN** the LLM call raises `LLMEmptyResponseError`
- **WHEN** `_request_turn()` catches it
- **THEN** it is classified `empty` with message starting `📭 Model returned no content`

#### Scenario: Context overflow error is classified as non-retryable
- **GIVEN** the LLM call raises `LLMContextOverflowError`
- **WHEN** `_request_turn()` catches it
- **THEN** it is classified `context`, starts with `📏 Context too long`, and remains non-retryable automatically

#### Scenario: Permanent error is classified as non-retryable
- **GIVEN** the LLM call raises `LLMPermanentError`
- **WHEN** `_request_turn()` catches it
- **THEN** it is classified `permanent`, starts with `❌`, and is not automatically retried

#### Scenario: Unknown error is classified as retryable
- **GIVEN** an exception matches no existing typed category
- **WHEN** `_request_turn()` catches it
- **THEN** it remains `unknown`, starts with `❌ LLM error:`, and is operator-retryable
- **AND** unknown errors are not automatically retried unless typed transient policy permits

#### Scenario: User cancellation is not classified or checkpointed
- **GIVEN** `/stop` raises `LLMCancelledError` during a request
- **WHEN** `_request_turn()` receives it
- **THEN** cancellation ends that request without classification, checkpoint, or error card

### Requirement: Disk-persisted checkpoint on LLM error

The system SHALL atomically write `data/run_checkpoints/{trace_id}.json` with `_LoopState` (messages, step, goal index, max steps, JSON failure streak), trace, goal, model, timestamp, and error info (type, message, retryability, detail). It SHALL persist attempt mode/count/remaining budget. `OSError` remains non-fatal.

#### Scenario: Checkpoint is written on LLM error
- **GIVEN** three tool calls completed and a later model request fails
- **WHEN** `_request_turn()` checkpoints the run
- **THEN** all tool results and existing loop metadata are stored
- **AND** error info records type, message, retryability, and detail
- **AND** pending attempt mode, attempt count, and remaining budget are stored

#### Scenario: Checkpoint write is atomic
- **GIVEN** a checkpoint is being written
- **WHEN** the write commits
- **THEN** it is written to a temporary file and atomically replaced
- **AND** a crash cannot leave a partial final checkpoint

#### Scenario: Checkpoint write failure is non-fatal
- **GIVEN** checkpoint writing raises `OSError`
- **WHEN** the retry prompt is prepared
- **THEN** a warning is logged and inline retry still proceeds
- **AND** `/resume` is unavailable if the checkpoint could not be written

#### Scenario: Checkpoint is deleted on successful run completion
- **GIVEN** a checkpoint exists and explicit recovery succeeds
- **WHEN** the resumed run completes
- **THEN** the checkpoint is deleted

#### Scenario: Checkpoint is deleted on explicit cancel
- **GIVEN** a checkpoint exists and the operator cancels
- **WHEN** cancellation is handled
- **THEN** the checkpoint is deleted

#### Scenario: Checkpoint survives retry-prompt timeout
- **GIVEN** an error card is waiting for operator action
- **WHEN** the retry-prompt timeout expires
- **THEN** the run unblocks and checkpoint remains for `/resume`

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

### Requirement: Scheduled job failures are classified and recorded

The system SHALL treat a scheduled job sub-agent result starting with `❌` or matching a known error type string as a failure, classify the error type, and preserve it with the next scheduled run time in the outcome. Failures are recorded in the execution log with a failure timestamp. No auto-retry SHALL be performed; the next cron run is the natural retry.

#### Scenario: Scheduled job failure is recorded in execution log
- **GIVEN** a scheduled job fails with an LLM error
- **WHEN** the failure is recorded in the execution log
- **THEN** the log entry includes the error type classification
- **AND** the log entry includes the failure timestamp
- **AND** when `notify=true`, its failure notice is deferred until the operator turn is idle and sent without notification sound

### Requirement: Scheduled failure notices are deferred and silent

For a **scheduled run** with `notify=true`, run-result notices SHALL wait until the **operator turn** is idle and typing has stopped, then be sent without sound; a failure notice includes `⚠️ Scheduled job failed: {tag}`, the error type, and next run time. With `notify=false`, run-result notices MUST be suppressed while outcomes remain recorded. Launch-failure alerts stay direct and independent of `notify`.

#### Scenario: Scheduled job timeout failure is classified and notified
- **GIVEN** a scheduled job returns a result string starting with `❌ LLM error: TimeoutException` and has `notify=true`
- **WHEN** the scheduler processes the failure while an operator turn is active
- **THEN** the failure is classified as a timeout error
- **AND** the failure outcome retains the error type and next scheduled run time
- **AND** a soundless `⚠️ Scheduled job failed: {tag}` notice including the error type and next run time is sent only after the operator turn is idle and typing has stopped

#### Scenario: Scheduled job success is notified normally
- **GIVEN** a scheduled job returns a successful result and has `notify=true`
- **WHEN** the scheduler processes the result
- **THEN** no error classification is applied
- **AND** its run-result notice waits until the operator turn is idle and typing has stopped
- **AND** the notice is sent without notification sound

#### Scenario: Muted scheduled failure has no run-result notice
- **GIVEN** a scheduled job with `notify=false` fails
- **WHEN** the scheduler records the classified failure
- **THEN** the error type and next scheduled run time remain in the recorded outcome
- **AND** no success or failure run-result notice is sent

#### Scenario: Muted scheduled success has no run-result notice
- **GIVEN** a scheduled job with `notify=false` succeeds
- **WHEN** the scheduler records the outcome
- **THEN** the outcome is retained
- **AND** no run-result notice is sent

#### Scenario: Launch-failure alert ignores notify setting
- **GIVEN** a scheduled job cannot launch and has `notify=false`
- **WHEN** the scheduler handles the launch failure
- **THEN** the existing direct launch-failure alert is sent independently of the run-result notification setting

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

### Requirement: Automatic retry settings belong to the agent

`[agent] max_retries` SHALL default to 4 additional attempts; `retry_delay` SHALL default to 2 seconds. Delays SHALL grow 2/4/8/16 seconds. Zero retries SHALL mean one initial call. Provider- or model-level retry settings SHALL be rejected.

#### Scenario: Retry defaults and zero are exact
- **GIVEN** default retry settings or `max_retries=0`
- **WHEN** an eligible transient request fails
- **THEN** defaults permit at most five total calls with stated delays
- **AND** zero permits exactly one outbound call

#### Scenario: Exhausted retry budget ends the run but not the bot
- **GIVEN** all permitted transient attempts are exhausted
- **WHEN** the current request terminates
- **THEN** the run ends
- **AND** Telegram commands and later independent tasks remain available

#### Scenario: Obsolete provider retry settings are rejected
- **GIVEN** provider or model configuration contains retry settings
- **WHEN** configuration is validated
- **THEN** it reports the obsolete key and directs retries to `[agent]`

### Requirement: One attempt state covers a logical model request

One `CallAttemptState` SHALL span control-flow calls until a usable response/action is accepted, terminal failure is finalized, or cancellation occurs. Empty responses, allowed parse re-prompts, mode conversions, and human/confirmation waits SHALL retain it. A later independent model call gets a new state.

#### Scenario: Control-flow loops retain request state
- **GIVEN** empty output, safe parse re-prompt, confirmation wait, or local native-to-JSON conversion occurs before acceptance
- **WHEN** the same logical response continues
- **THEN** its attempt count and remaining budget are retained

#### Scenario: Later model call receives a new state
- **GIVEN** a usable action is accepted and its tool result is recorded
- **WHEN** the next independent model response is needed
- **THEN** it receives a new attempt state with prior tool results retained

#### Scenario: Effects and partial output prohibit automatic retry
- **GIVEN** a failed call emitted partial output or caused an effect
- **WHEN** failure handling begins
- **THEN** no automatic retry or whole-run replay occurs
- **AND** a later failed call can still retry after earlier completed tool turns

#### Scenario: Explicit checkpoint recovery does not refill budget
- **GIVEN** Retry or `/resume` restores a checkpoint
- **WHEN** the saved request continues
- **THEN** the same state and remaining automatic budget are restored
- **AND** no completed tool is re-executed
