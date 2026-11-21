## ADDED Requirements

### Requirement: Interactive LLM recovery is explicitly enabled per run

The system SHALL default each run's interactive LLM recovery capability to false. Telegram root requests and Telegram resume SHALL explicitly enable it. On-demand, scheduled, plan-step and diagnostic runners SHALL remain headless for LLM recovery regardless of callback presence, notification delivery, depth or label. This capability SHALL NOT govern sensitive-tool confirmation.

#### Scenario: Real headless runner exhausts automatic attempts
- **GIVEN** a headless runner encounters a transient LLM failure
- **WHEN** its existing automatic request policy is exhausted
- **THEN** it returns a sanitized terminal failure without invoking an operator retry wait, publishing a retry token or saving a new recovery checkpoint
- **AND** the outbound attempt count remains bounded by the existing request budget

#### Scenario: Notification callbacks do not enable recovery
- **WHEN** a headless runner can send notifications or bridge an allowed tool confirmation
- **THEN** its LLM recovery capability remains false
- **AND** existing tool-confirmation policy is unchanged

#### Scenario: Telegram root and resume retain interaction
- **WHEN** Telegram invokes a root request or eligible resume
- **THEN** the per-run capability is true and retryable failures retain interactive recovery
- **AND** concurrent headless runs do not inherit that capability

### Requirement: Terminal LLM failures end safely and propagate as failures

After the existing automatic policy finishes, non-retryable and headless terminal LLM failures SHALL return immediate fixed, sanitized guidance without a new checkpoint or card. Cancellation SHALL win. Callers SHALL report failure without whole-task replay, preserving cleanup, pre-send recovery data and scheduler policy. The scenarios below define guidance and outcome normalization.

#### Scenario: Context or permanent failure is terminal
- **WHEN** an interactive or headless request fails as context or permanent
- **THEN** fixed actionable failure guidance is returned without a retry wait, token, new checkpoint or manual retry authorization
- **AND** completed tool effects are not replayed
- **AND** context guidance SHALL recommend reducing request/context, while permanent guidance SHALL recommend checking model/provider configuration

#### Scenario: Terminal guidance is safe and recognizable
- **WHEN** any terminal LLM failure is returned
- **THEN** its fixed category-selected string SHALL begin with `❌` and SHALL NOT interpolate arbitrary provider or exception text
- **AND** it SHALL claim automatic-budget exhaustion only when that budget was exhausted

#### Scenario: Cancellation wins at failure handling
- **GIVEN** cancellation is signalled when terminal error handling begins
- **WHEN** the run ends
- **THEN** it reports cancellation rather than LLM failure and does not clear a shared cancellation signal
- **AND** owned clients, leases and registrations are released and child draining remains intact

#### Scenario: Older retryable state survives preparation failure
- **GIVEN** an eligible snapshot exists and resume has not sent a new request
- **WHEN** preparation fails or cancellation occurs
- **THEN** the saved snapshot remains unchanged and no replacement non-retryable checkpoint is written
- **AND** existing exact-snapshot retirement immediately before a new send remains unchanged

#### Scenario: Runtime failure reaches all callers truthfully
- **WHEN** a runner returns a terminal `❌` failure
- **THEN** supervisor, parent retrieval, plan and scheduler report failure rather than success
- **AND** plan normalization sets `success=false` and `recoverable=false` without whole-task retry or diagnostic replay
- **AND** scheduler classification, next-cron behavior, recording and notification rules SHALL remain unchanged

#### Scenario: Caller normalization respects existing outcome precedence
- **WHEN** supervisor or plan normalization receives a runner result
- **THEN** it SHALL honor cancellation first, existing explicit structured outcomes second, and the reserved plain-string `❌` prefix third
- **AND** an unstructured string beginning with that prefix SHALL mean failure, not successful prose

#### Scenario: Successful structured output retains its content
- **WHEN** a runner returns an existing explicit successful JSON outcome whose output begins with `❌`
- **THEN** supervisor and plan normalization preserve its success and output
- **AND** ordinary successful non-prefixed text remains successful

#### Scenario: Failure without automatic retry does not claim exhaustion
- **WHEN** a headless unknown failure terminates without exhausting an automatic budget
- **THEN** its fixed guidance reports the failure without claiming automatic retries were exhausted

## MODIFIED Requirements

### Requirement: Disk-persisted checkpoint on LLM error

For retryable LLM failures in runs explicitly capable of interactive LLM recovery, the system SHALL atomically write `data/run_checkpoints/{trace_id}.json` when checkpoint writing is enabled. The checkpoint SHALL include `_LoopState` (messages, step, goal index, max steps, JSON failure streak), trace, goal, model, timestamp, error info (type, message, retryability, detail) and attempt mode/count/remaining budget. Headless and non-retryable failures SHALL NOT create new recovery checkpoints. `OSError` SHALL remain non-fatal. Existing ownership and exact-snapshot send-gate retirement SHALL remain enforced.

#### Scenario: Checkpoint is written on LLM error
- **GIVEN** three tool calls completed and a later interactive model request fails retryably
- **WHEN** the system checkpoints the run
- **THEN** all tool results, existing loop metadata, error info and pending attempt state are stored

#### Scenario: Checkpoint write is atomic
- **WHEN** an eligible checkpoint write commits
- **THEN** a temporary file is atomically replaced and a crash cannot leave a partial final checkpoint

#### Scenario: Checkpoint write failure is non-fatal
- **WHEN** eligible checkpoint writing raises `OSError`
- **THEN** a warning is logged and inline retry still proceeds
- **AND** the user is told that disk recovery is unavailable rather than promised `/resume`

#### Scenario: Checkpoint is deleted on successful run completion
- **GIVEN** a checkpoint exists and explicit recovery succeeds
- **WHEN** the resumed run completes
- **THEN** its checkpoint is retired under existing ownership rules

#### Scenario: Checkpoint is deleted on explicit cancel
- **GIVEN** an eligible checkpoint exists
- **WHEN** the operator explicitly cancels the recovery prompt
- **THEN** the checkpoint is retired under existing ownership rules

#### Scenario: Checkpoint survives retry-prompt timeout
- **WHEN** an eligible interactive retry prompt times out
- **THEN** the run unblocks and its checkpoint remains for `/resume`

## ADDED Requirements

### Requirement: Inline retry prompt for eligible interactive failures

Only explicitly interactive runs with retryable LLM failures SHALL block on a Retry/Cancel card, using the configured timeout (default 120 seconds). Cards SHALL show the classified icon/message, model, step/max steps, preserved tool-result count and at most 200 characters of safe error detail. Non-retryable and headless failures SHALL return guidance without any card or wait.

#### Scenario: Retryable error shows Retry and Cancel buttons
- **GIVEN** an interactive timeout error occurs and its checkpoint is written
- **WHEN** Telegram renders the error card
- **THEN** it shows the timeout icon/message, model, step/max steps and preserved-tool-result count
- **AND** it shows Retry and Cancel buttons
- **AND** it shows at most the first 200 characters of safe error detail

#### Scenario: Non-retryable error has no blocking card
- **WHEN** context overflow or permanent rejection occurs
- **THEN** the run returns fixed actionable guidance without waiting for operator input
- **AND** no Retry or Cancel-only error card is shown

#### Scenario: User presses Retry and run resumes from checkpoint
- **GIVEN** a retryable interactive error card is showing
- **WHEN** the operator presses Retry
- **THEN** the thread unblocks and continues the saved step/messages with the same attempt state
- **AND** prior tool results are not re-executed
- **AND** a new LLM call is made with the full conversation context

#### Scenario: User presses Cancel and checkpoint is deleted
- **GIVEN** a retryable interactive error card is showing
- **WHEN** the operator presses Cancel
- **THEN** the thread unblocks, retires its checkpoint under existing ownership rules and returns an error string to Telegram

#### Scenario: Retry prompt times out without user action
- **WHEN** an interactive retry card receives no decision before its configured timeout
- **THEN** the run unblocks with an error string and leaves its checkpoint for `/resume`

#### Scenario: New message during retry prompt is deferred
- **WHEN** a new message arrives while an interactive retry prompt is active
- **THEN** existing deferred-message behavior presents it after the current run ends

### Requirement: /resume command for eligible checkpoint recovery

The system SHALL provide `/resume` for eligible main-agent checkpoints only. The controller/recovery layer SHALL own eligibility and SHALL reject a valid checkpoint marked `error_info.retryable=false` before manual retry authorization or any send; Telegram SHALL apply the same rule for presentation only. `/resume` and `/resume N` SHALL use one filtered, newest-first, contiguously numbered list. Resume SHALL NOT delete ineligible files, refill the automatic budget or replay completed tools.

#### Scenario: /resume with single checkpoint resumes immediately
- **GIVEN** one eligible checkpoint from a prior timeout exists
- **WHEN** the operator runs `/resume`
- **THEN** the system loads its saved state and displays "💾 Resuming: {goal} (step {N}/{max})"

#### Scenario: /resume with multiple checkpoints lists them
- **WHEN** `/resume` finds multiple eligible checkpoints
- **THEN** it lists goal, step/max, error type and age with contiguous numbers
- **AND** it prompts the operator to use `/resume N`
- **AND** the newest eligible checkpoint is listed first

#### Scenario: /resume N selects a specific checkpoint
- **GIVEN** newest-to-oldest files contain retryable A, non-retryable B, retryable C
- **WHEN** `/resume` lists them and the operator selects `/resume 2`
- **THEN** only A and C are numbered and C is selected
- **AND** B remains unchanged on disk

#### Scenario: /resume with no checkpoints informs the user
- **WHEN** there are no eligible checkpoints, including when only non-retryable files exist
- **THEN** `/resume` reports "No unfinished runs to resume."

#### Scenario: /resume while agent is busy is rejected
- **WHEN** `/resume` is invoked while the agent is busy
- **THEN** the response says "⚠️ Agent is currently running. Wait for it to finish or /stop it first."

#### Scenario: /resume preserves corrupted checkpoint files
- **GIVEN** a checkpoint has invalid JSON or missing required fields
- **WHEN** existing corrupted-checkpoint handling runs
- **THEN** enumeration skips files it cannot parse or validate, and an explicit resume attempt is refused under existing validation/ownership rules
- **AND** the corrupted file remains unchanged on disk, without automatic deletion
- **AND** remaining valid eligible checkpoints are available

#### Scenario: /resume refuses non-retryable checkpoint
- **WHEN** any frontend or direct controller caller explicitly attempts a non-retryable checkpoint
- **THEN** controller/recovery rejects it with fixed guidance before authorization or sending
- **AND** the checkpoint remains unchanged
- **AND** no deletion action is offered

#### Scenario: Legacy checkpoint omits retryability
- **WHEN** an otherwise valid older checkpoint omits `error_info.retryable`
- **THEN** it remains eligible subject to existing resume validation

#### Scenario: /resume is main-agent only
- **WHEN** `/resume` enumerates checkpoints
- **THEN** only eligible main-agent checkpoints are available, never sub-agent checkpoints

#### Scenario: Resumed run reuses the checkpoint's trace_id
- **GIVEN** an eligible checkpoint has trace `r-a1b2c3d4`
- **WHEN** its resumed run succeeds
- **THEN** it uses that trace for correlation and retires that checkpoint under existing ownership rules
- **AND** it continues the stored attempt state without refilling the automatic budget or replaying completed tools

## MODIFIED Requirements

### Requirement: Startup checkpoint scan notification

Startup SHALL announce only eligible main-agent checkpoints using the same recovery-layer policy as `/resume`. With no eligible checkpoints it SHALL send no recovery announcement. With multiple eligible checkpoints it SHALL mention the newest eligible goal and direct the operator to `/resume`. Non-retryable files SHALL NOT be deleted, changed or announced as recoverable.

#### Scenario: Startup with existing checkpoints sends notification
- **WHEN** startup finds one eligible checkpoint
- **THEN** the operator receives "💾 Found unfinished run: '{goal}'. Send /resume to continue."

#### Scenario: Startup with no checkpoints sends no notification
- **WHEN** startup finds no eligible checkpoints, including a directory containing only non-retryable files
- **THEN** no recovery notification is sent and excluded files remain untouched

#### Scenario: Startup with multiple checkpoints notifies about the most recent
- **GIVEN** the newest stored checkpoint is non-retryable and two older eligible checkpoints exist
- **WHEN** startup scans
- **THEN** it announces the newest eligible goal and directs the operator to `/resume` for available runs

### Requirement: LLM error handling configuration

The system SHALL retain `[llm_error_handling] retry_timeout_seconds` (integer, default 120) and `checkpoint_enabled` (boolean, default true). These settings SHALL NOT grant interactive LLM recovery capability. When checkpoint writing is disabled, eligible interactive failures SHALL still allow in-memory Retry/Cancel, but no new disk recovery checkpoint SHALL be available. Existing stored eligible checkpoints SHALL follow existing resume validation.

#### Scenario: Default config applies when section is absent
- **WHEN** the configuration omits `[llm_error_handling]`
- **THEN** retry timeout defaults to 120 seconds and checkpoint writing defaults to true

#### Scenario: Checkpoint disabled still allows inline retry
- **GIVEN** checkpoint writing is disabled and no prior checkpoints exist
- **WHEN** an explicitly interactive request fails retryably
- **THEN** no checkpoint is written but Retry/Cancel remains available from memory
- **AND** `/resume` reports no unfinished runs

#### Scenario: Configuration does not enable headless waits
- **WHEN** a headless request fails with checkpoint writing enabled and a positive retry timeout
- **THEN** it still returns terminal failure without a retry card, wait or new checkpoint

## REMOVED Requirements

### Requirement: Inline retry prompt with error card
**Reason**: Replaced by "Inline retry prompt for eligible interactive failures"; the former requirement mandates a blocking Cancel-only card for non-retryable failures, which is intentionally retired.

**Migration**: Keep interactive retryable Retry/Cancel behavior under the replacement requirement. Non-retryable failures return immediate fixed guidance without a card or wait.

### Requirement: /resume command for checkpoint recovery
**Reason**: Replaced by "/resume command for eligible checkpoint recovery"; the former requirement specified corrupted-checkpoint deletion and a non-retryable deletion offer, contrary to the approved non-destructive handling.

**Migration**: Retain all other resume behavior under the replacement requirement. Corrupted and non-retryable checkpoint files remain unchanged; no automatic cleanup or deletion interface is introduced.
