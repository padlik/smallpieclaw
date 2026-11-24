## MODIFIED Requirements

### Requirement: /resume command for eligible checkpoint recovery

The system SHALL provide `/resume` for eligible main-agent checkpoints only. The controller/recovery layer SHALL own eligibility and SHALL reject a valid checkpoint marked `error_info.retryable=false` before manual retry authorization or any send; Telegram SHALL apply the same rule for presentation only. `/resume` and `/resume N` SHALL use one filtered, newest-first, contiguously numbered list. Resume SHALL NOT delete ineligible files, refill the automatic budget or replay completed tools. Each resumed execution SHALL admit the current active model once, preserving the saved state and historical identity without requiring model equality. An unavailable or inadmissible active model SHALL cause actionable local failure without sending or changing the checkpoint.

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

#### Scenario: Active model differs from checkpoint model
- **GIVEN** an eligible checkpoint created with `a/model` and active selection `b/model`
- **WHEN** `/resume` admits the saved task
- **THEN** it continues on `b/model` without requiring `a/model` to remain configured or callable
- **AND** saved messages, completed results and loop position are restored without executing prior tools

#### Scenario: No active model can be admitted
- **GIVEN** an eligible checkpoint and an unset or inadmissible active selection
- **WHEN** `/resume` is attempted
- **THEN** actionable selection/configuration guidance is returned without a provider send
- **AND** the checkpoint bytes remain unchanged

### Requirement: One attempt state covers a logical model request

One `CallAttemptState` SHALL span control-flow calls until a usable response/action is accepted, terminal failure is finalized, or cancellation occurs. Empty responses, allowed parse re-prompts, mode conversions, and human/tool-confirmation waits SHALL retain it. A later independent model call gets a new state. Explicit `/resume`, including a model change, SHALL restore saved counters and grant at most the existing single manual continuation without refilling automatic attempts.

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
- **GIVEN** `/resume` restores a checkpoint, on the same or a different active model
- **WHEN** the saved request continues
- **THEN** the same state and remaining automatic budget are restored with at most the existing single manual continuation authorization
- **AND** no completed tool is re-executed

### Requirement: Terminal LLM failures end safely and propagate as failures

After the existing automatic policy finishes, all terminal LLM failures SHALL return fixed, sanitized guidance without an operator-input wait or card. Eligible retryable interactive failures SHALL follow durable checkpoint persistence and truthfully state recovery availability; non-retryable and headless failures SHALL NOT create new recovery checkpoints. Cancellation SHALL win. Callers SHALL report failure without whole-task replay, preserving cleanup, pre-send recovery data and scheduler policy. The scenarios below define guidance and outcome normalization.

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
- **THEN** the saved snapshot remains unchanged and no replacement checkpoint is written
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

#### Scenario: Interactive failure releases execution without a card wait
- **GIVEN** an eligible Telegram run exhausts its automatic policy on a retryable failure
- **WHEN** checkpoint saving finishes or fails
- **THEN** the run returns truthful recovery guidance without publishing a retry token or awaiting a decision
- **AND** normal cleanup closes owned clients and releases checkpoint ownership after existing child draining

## ADDED Requirements

### Requirement: Durable checkpoints preserve eligible failed requests

Eligible retryable LLM failures SHALL atomically save `data/run_checkpoints/{trace_id}.json` when writing is enabled, then end without an operator wait. Headless and non-retryable failures SHALL NOT create new checkpoints. `OSError` SHALL remain non-fatal. Existing ownership and exact-snapshot send-gate retirement SHALL remain enforced. Payload, availability and abandoned-file rules follow the scenarios below.

#### Scenario: Checkpoint is written on LLM error
- **GIVEN** three tool calls completed and a later eligible model request fails retryably
- **WHEN** the system checkpoints the run
- **THEN** all tool results, existing loop metadata, error info and pending attempt state are stored
- **AND** the payload SHALL include messages, step, goal index, max steps, JSON failure streak, trace, goal, model, timestamp, error type/message/retryability/detail and attempt mode/count/remaining budget
- **AND** the failed run returns without waiting for operator input

#### Scenario: Checkpoint write is atomic
- **WHEN** an eligible checkpoint write commits
- **THEN** a temporary file is atomically replaced and a crash cannot leave a partial final checkpoint

#### Scenario: Checkpoint write failure is non-fatal
- **WHEN** eligible checkpoint writing raises `OSError` or returns failure
- **THEN** a warning is logged and the run ends with sanitized failure and recovery-unavailable guidance
- **AND** no memory-only continuation or `/resume` promise is offered for that failed request

#### Scenario: Checkpoint is deleted on successful run completion
- **GIVEN** a checkpoint exists and explicit recovery succeeds
- **WHEN** the resumed run completes
- **THEN** its checkpoint is retired under existing ownership rules

#### Scenario: Recovery guidance requires verified saved state
- **GIVEN** an eligible retryable request fails
- **WHEN** the owner-backed save returns success and exact-snapshot recovery verification succeeds
- **THEN** the failure guidance offers `/resume`
- **AND** a save failure or failed verification instead reports recovery unavailable

#### Scenario: Abandoned checkpoint remains listed
- **GIVEN** an eligible checkpoint is saved and the failed run has ended
- **WHEN** the operator does not resume it
- **THEN** removal of live cards introduces no automatic deletion or expiry
- **AND** existing eligible enumeration and startup notification still apply
- **AND** abandoned files SHALL remain subject to existing safe retirement or operator-managed cleanup, without a new discard command or cleanup mechanism

### Requirement: Durable recovery configuration excludes live retry waits

The system SHALL retain `[llm_error_handling] checkpoint_enabled` (boolean, default true). It SHALL reject `llm_error_handling.retry_timeout_seconds` with any value, naming the field and directing its removal because blocking Retry cards were removed. Configuration SHALL NOT grant per-run checkpoint eligibility. Disabled-write behavior follows the scenarios below.

#### Scenario: Default config applies when section is absent
- **WHEN** the configuration omits `[llm_error_handling]`
- **THEN** checkpoint writing defaults to true and no live retry timeout is created

#### Scenario: Checkpoint disabled provides no new recovery
- **GIVEN** checkpoint writing is disabled and no prior checkpoints exist
- **WHEN** an eligible Telegram request fails retryably
- **THEN** it ends without writing a checkpoint or showing Retry/Cancel
- **AND** it reports recovery unavailable and `/resume` reports no unfinished runs
- **AND** disabling writing SHALL provide no memory-only recovery

#### Scenario: Configuration does not enable headless waits
- **WHEN** a headless request fails with checkpoint writing enabled
- **THEN** it returns terminal failure without a retry card, wait or new checkpoint

#### Scenario: Obsolete timeout setting is rejected
- **WHEN** configuration contains `llm_error_handling.retry_timeout_seconds` with any value
- **THEN** validation names the field and directs its removal because blocking Retry cards were removed
- **AND** the setting is neither ignored nor repurposed

#### Scenario: Disabled writes do not disable existing recovery
- **GIVEN** an eligible checkpoint exists and new checkpoint writing is disabled
- **WHEN** `/resume` continues it
- **THEN** existing validation, attempt accounting and exact-snapshot pre-send retirement still apply
- **AND** disabled new writes SHALL NOT prevent recovery of that existing eligible checkpoint

### Requirement: Durable checkpoint recovery eligibility is explicitly enabled per run

The system SHALL default each run's durable checkpoint recovery eligibility to false. Telegram root requests and Telegram resume SHALL explicitly enable it. On-demand, scheduled, plan-step and diagnostic runners SHALL remain ineligible regardless of callback presence, notification delivery, depth or label. This eligibility SHALL NOT govern sensitive-tool confirmation and SHALL NOT enable any LLM error retry wait.

#### Scenario: Real headless runner exhausts automatic attempts
- **GIVEN** a headless runner encounters a transient LLM failure
- **WHEN** its existing automatic request policy is exhausted
- **THEN** it returns a sanitized terminal failure without invoking an operator retry wait, publishing a retry token or saving a new recovery checkpoint
- **AND** the outbound attempt count remains bounded by the existing request budget

#### Scenario: Notification callbacks do not enable recovery
- **WHEN** a headless runner can send notifications or bridge an allowed tool confirmation
- **THEN** its durable checkpoint recovery eligibility remains false
- **AND** existing tool-confirmation policy is unchanged

#### Scenario: Telegram root and resume explicitly enable checkpoint eligibility
- **WHEN** Telegram invokes a root request or eligible resume
- **THEN** its per-run eligibility is true and retryable failures can save durable recovery state when writing is enabled
- **AND** no live retry card is shown and concurrent headless runs do not inherit eligibility

### Requirement: Resume model handoff is announced safely

When canonical model IDs differ, recovery SHALL announce both models and the saved-context provider handoff after the ordinary goal/step resume notice and before model I/O. The notice SHALL use existing safe rendering/redaction, add no blocking approval, and expose no credentials, endpoints or raw checkpoint/provider error content.

#### Scenario: Cross-provider handoff is announced
- **GIVEN** an eligible checkpoint from `a/model` and an admitted active target `b/model`
- **WHEN** resume validates the saved state
- **THEN** a handoff notice naming both models and explaining saved-context delivery follows the ordinary resume notice before provider I/O
- **AND** it SHALL explain that saved conversation and tool results will be sent to the selected model's provider
- **AND** execution does not wait for another approval

#### Scenario: Same-model resume does not imply original-client restoration
- **GIVEN** checkpoint and admitted canonical model IDs match
- **WHEN** resume starts
- **THEN** the ordinary resume notice remains and no model-change notice is required
- **AND** no original-client restoration guarantee is made

#### Scenario: Notification failure does not duplicate execution
- **WHEN** handoff-notice delivery fails
- **THEN** notification failure SHALL NOT trigger repeated execution, second model admission or alternate target

### Requirement: Stale live retry callbacks are inert

Old LLM Retry/Cancel callbacks SHALL follow existing authentication, acknowledge their expired controls and direct the operator to `/resume`. They SHALL NOT resume execution, grant retry authorization, delete or rewrite checkpoints, or claim that retry started. New executions SHALL NOT publish live LLM retry tokens or render replacement Resume buttons.

#### Scenario: Old Retry and Cancel buttons cannot mutate recovery
- **GIVEN** an authenticated operator presses a Retry or Cancel button from before upgrade
- **WHEN** its callback is handled
- **THEN** it is acknowledged as expired with `/resume` guidance
- **AND** no run starts, retry budget changes or checkpoint mutation occurs

#### Scenario: Unauthenticated stale callback discloses no recovery information
- **WHEN** an unauthenticated user sends an old LLM retry callback
- **THEN** existing authentication denies access without disclosing checkpoint data or mutating state

## REMOVED Requirements

### Requirement: Inline retry prompt for eligible interactive failures

**Reason**: Blocking live cards duplicate durable recovery, retain failed executions and support memory-only continuation that is intentionally removed.

**Migration**: Eligible failures save progress and return immediately; use `/resume` or `/resume N` with the active model. Remove the obsolete retry timeout key. Card-specific Cancel, timeout and deferred-message scenarios disappear with the card; the Cancel-deletion and retry-prompt-timeout-survival scenarios within “Disk-persisted checkpoint on LLM error” are also intentionally retired. General deferred-message and tool-confirmation behavior remains unchanged. Failed/disabled checkpoint writing provides no memory-only recovery.

### Requirement: Disk-persisted checkpoint on LLM error

**Reason**: The former lifecycle couples disk persistence to button Cancel and retry-prompt timeout, which are intentionally removed.

**Migration**: Replaced by 'Durable checkpoints preserve eligible failed requests'; atomic state, ownership and safe retirement remain, but live-card Cancel-deletion and timeout-survival scenarios are retired.

### Requirement: LLM error handling configuration

**Reason**: Timeout configuration and memory-only inline retry are removed.

**Migration**: Use 'Durable recovery configuration excludes live retry waits'; retain checkpoint_enabled, remove retry_timeout_seconds, and use saved checkpoints through /resume.

### Requirement: Interactive LLM recovery is explicitly enabled per run

**Reason**: Live interaction is removed while explicit checkpoint eligibility remains necessary.

**Migration**: Use 'Durable checkpoint recovery eligibility is explicitly enabled per run'; root/resume remain eligible and headless runs remain ineligible, without a Retry card.
