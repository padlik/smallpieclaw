## MODIFIED Requirements

### Requirement: Run admission pins one immutable target without fallback

Every run SHALL be admitted exactly once, resolving the connection credential once and pinning one immutable target of connection, endpoint, adapter, and exact upstream ID for the whole run. Later selection changes or refreshes SHALL NOT change it, and runtime construction SHALL NOT re-admit. A well-formed upstream ID on a configured connection SHALL be sent even when undiscovered; provider rejection SHALL be a normal failure with no fallback, substitution, or retry on another model. Explicit checkpoint resume SHALL be a newly admitted execution using the current active selection, not a restoration of the original client's pin; its trace and saved logical-request state SHALL remain continuous.

#### Scenario: Unknown upstream is attempted and rejection is not hidden
- **GIVEN** connection `openai-prod` is configured
- **WHEN** a run names `openai-prod/does-not-exist` and the provider rejects it
- **THEN** the rejection is reported as a normal run failure
- **AND** no other model is tried

#### Scenario: Selection change does not affect a running task
- **GIVEN** a run is pinned to `openai-prod/gpt-x`
- **WHEN** the operator selects `anthropic-main/claude-y` during the run
- **THEN** the running task keeps `openai-prod/gpt-x`
- **AND** new implicit admissions use `anthropic-main/claude-y`

#### Scenario: Refresh does not affect a running task
- **GIVEN** a run is pinned to a model and refresh removes that model from the cache
- **WHEN** the run makes its next provider call
- **THEN** it uses the same pinned connection and upstream ID

#### Scenario: Resumed execution keeps its newly admitted target
- **GIVEN** a checkpoint created with `a/model` resumes on active `b/model`
- **WHEN** selection later changes to `c/model` or discovery removes `b/model`
- **THEN** all requests in that resumed execution remain pinned to `b/model`
- **AND** admission occurs once and resume changes neither active selection nor preference

## ADDED Requirements

### Requirement: Resume admits the active model while preserving recovery safety

A checkpoint SHALL resume on the active model admitted once for that execution, without historical-model equality. Recovery SHALL preserve validation, ownership, attempt accounting, completed results, pre-send snapshot preservation and retirement as specified below. Client cleanup and cancellation SHALL retain existing ownership. A model change SHALL be announced under `llm-error-recovery`.

#### Scenario: Checkpoint resume uses the active model and does not replay tools
- **GIVEN** a run pinned to `a/model` was checkpointed after completed tool calls and active selection is now `b/model`
- **WHEN** the checkpoint is resumed
- **THEN** the resumed request uses newly admitted `b/model` and saved attempt state, without resetting counters
- **AND** no completed tool is re-executed and active selection/preference remain unchanged
- **AND** the historical connection SHALL NOT need to remain configured, and the automatic budget SHALL NOT be refilled
- **AND** only the new target's own credential SHALL reach its endpoint

#### Scenario: Active-model handoff retains recovery validation
- **GIVEN** an explicit resume on a different active model
- **WHEN** recovery validates and restores the saved task
- **THEN** checkpoint/history/type/trace validation and process-local ownership SHALL remain enforced
- **AND** historical checkpoint identity SHALL NOT be rewritten or pre-send snapshot preservation weakened to accommodate the target change

#### Scenario: Cancellation is not a model failure
- **GIVEN** a pinned run is cancelled during a provider call
- **WHEN** cancellation propagates
- **THEN** the run's client is closed by its owner and no error classification or checkpoint is produced

#### Scenario: Existing native history crosses supported adapters without replay
- **GIVEN** a checkpoint includes completed native tool calls/results and the new active target uses a supported different adapter
- **WHEN** the saved task continues through existing serialization or local pre-send native-to-JSON conversion
- **THEN** preserved tool results reach the selected target in its supported representation without executing completed tools
- **AND** local conversion does not consume an outbound attempt or reset its saved state

#### Scenario: Unsupported saved input does not trigger model fallback
- **GIVEN** a valid checkpoint and an admitted active model that rejects its context, images or tool history
- **WHEN** request preparation or provider execution fails
- **THEN** existing sanitized failure handling applies without alternate-model selection or task restart
- **AND** a pre-send failure preserves the original snapshot unchanged, while an actual send retires that exact snapshot before transport
- **AND** retirement SHALL occur immediately before actual transport send

#### Scenario: Retirement failure blocks cross-model send
- **GIVEN** a resumed checkpoint changes on disk or its exact-snapshot retirement fails
- **WHEN** the new target is about to send
- **THEN** no provider request is transmitted and the saved file is not overwritten
- **AND** the run releases its client and ownership under existing cleanup

#### Scenario: New post-send failure records the new model only when eligible
- **GIVEN** a checkpoint from `a/model` resumes on `b/model` and its exact snapshot is retired before send
- **WHEN** the sent request fails retryably with checkpoint writing enabled for an eligible run
- **THEN** any fresh checkpoint records `b/model` with the updated saved attempt state
- **AND** a non-retryable post-send failure does not resurrect the retired `a/model` snapshot

## REMOVED Requirements

### Requirement: Run ownership and recovery contracts are unchanged

**Reason**: Historical-model continuity is intentionally replaced by current-active-model admission for explicit recovery.

**Migration**: Use 'Resume admits the active model while preserving recovery safety'; preserve completed results, attempt budgets, ownership, client cleanup and send-gate retirement while announcing any changed model/provider.
