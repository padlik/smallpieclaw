## MODIFIED Requirements

### Requirement: Compaction summaries use the pinned client with their own attempt state

Each automatic compaction summary SHALL be requested through the run's pinned client with its own zero-retry attempt state and existing usage accounting. Each triggered summary SHALL make at most one outbound attempt, with no automatic retry, manual continuation, or backoff wait, regardless of the main request's configured retry allowance. The summary SHALL leave the main request's attempt state and checkpoint unchanged.

#### Scenario: Summary uses the pinned client with its own attempt state
- **GIVEN** a run is pinned to one target
- **WHEN** an automatic compaction summary is requested
- **THEN** it is sent through the same pinned client with its own zero-retry attempt state and existing usage accounting
- **AND** the main request's attempt state and checkpoint are not changed by it

#### Scenario: A transient summary failure does not retry or wait
- **GIVEN** the main request permits two automatic retries
- **WHEN** the automatic summary's first outbound attempt raises a transient provider error
- **THEN** exactly one summary outbound attempt is recorded and no summary retry wait occurs
- **AND** the original main request retains both of its permitted automatic retries

#### Scenario: Successful compaction does not disable later summaries
- **GIVEN** an automatic compaction summary has succeeded
- **WHEN** a later request in the same run exceeds the threshold with an eligible middle
- **THEN** a new independent one-shot summary attempt is allowed
- **AND** no extra compaction is performed in the earlier request's step

### Requirement: Automatic compaction failure preserves history and disables further automatic attempts for the run

When an automatic summary's single outbound call fails with a supported provider error, returns empty, whitespace-only, or non-string output, or yields an invalid candidate, the system SHALL keep the original history and goal index, log a sanitized warning, continue the original request, and disable automatic compaction for that run only. It SHALL NOT retry the failed summary or wait for summary backoff. Cancellation before or during the summary call, racing a supported provider failure, or after the reply before publication SHALL propagate as run cancellation with history and goal index unchanged, no subsequent main request send, and no compaction-failure warning, classification, or failure-driven disablement. Unexpected programming errors SHALL propagate unchanged rather than being converted into supported compaction failures.

#### Scenario: Summary call fails
- **GIVEN** the summary call raises a supported provider error on its single outbound attempt
- **WHEN** automatic compaction handles it
- **THEN** the original history and goal index are preserved and the original main request is sent unchanged with a sanitized warning
- **AND** later over-threshold requests in that run make no automatic compaction attempt

#### Scenario: Empty summary is a failure
- **GIVEN** the summary call returns only whitespace
- **WHEN** automatic compaction handles it
- **THEN** no history or goal index is replaced and further automatic attempts in the run are disabled
- **AND** the original request continues without another summary attempt

#### Scenario: Non-string summary is a failure
- **GIVEN** the summary call returns a non-string value
- **WHEN** automatic compaction handles it
- **THEN** the original history and goal index are preserved, a sanitized warning is logged, and automatic compaction is disabled for this run
- **AND** the original request continues without another summary attempt

#### Scenario: Invalid compaction candidate is a failure
- **GIVEN** a non-empty summary yields a candidate that fails native-history validation
- **WHEN** automatic compaction handles it
- **THEN** the original history and goal index are preserved, a sanitized warning is logged, and automatic compaction is disabled for this run
- **AND** the original request continues without another summary attempt

#### Scenario: Cancellation ends the run without a failure record
- **GIVEN** the operator cancels during a summary call
- **WHEN** cancellation is raised
- **THEN** the run ends through existing cancellation handling with history and goal index unchanged and no subsequent main request send
- **AND** no compaction-failure warning, classification, or failure-driven disablement is produced

#### Scenario: Cancellation before summary send prevents provider I/O
- **GIVEN** cancellation is set before the summary helper begins its call
- **WHEN** the helper handles the pending cancellation
- **THEN** no summary or subsequent main request is sent and history and goal index stay unchanged
- **AND** no compaction-failure warning, classification, or failure-driven disablement is produced

#### Scenario: Cancellation racing provider failure wins
- **GIVEN** cancellation is set while the summary call raises a supported provider error
- **WHEN** the helper handles that error
- **THEN** cancellation propagates with history and goal index unchanged and no subsequent main request send
- **AND** no compaction-failure warning, classification, or failure-driven disablement is produced

#### Scenario: Cancellation after reply prevents publication
- **GIVEN** the summary reply arrives and cancellation is set before candidate publication
- **WHEN** automatic compaction handles the reply
- **THEN** cancellation propagates with history and goal index unchanged and no subsequent main request send
- **AND** no compaction-failure warning, classification, or failure-driven disablement is produced

#### Scenario: A new run re-enables automatic compaction
- **GIVEN** automatic compaction was disabled by a failure in a previous run
- **WHEN** a new run exceeds the threshold
- **THEN** it makes an automatic compaction attempt

#### Scenario: Unexpected programming errors are not summary failures
- **GIVEN** summarization raises an unexpected programming error rather than a supported provider error
- **WHEN** the compaction path handles it
- **THEN** the error propagates unchanged without a compaction-failure warning or failure-driven disablement
