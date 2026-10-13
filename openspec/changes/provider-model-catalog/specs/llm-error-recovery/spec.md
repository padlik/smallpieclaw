# LLM Error Recovery Specification

## Purpose

Add bounded per-agent attempts while retaining typed classification, checkpoint recovery, operator choices, and scheduled-failure behavior.

## MODIFIED Requirements

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

## ADDED Requirements

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
